"""LiveViewer desktop interface and bounded asynchronous media orchestration."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QListView, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QSplitter, QStackedWidget, QStyle, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QWidgetAction,
)

from .cache import CacheStore, file_version
from .cache_locations import open_cache
from .catalog_model import CatalogModel, ThumbnailDelegate
from .imaging import load_image
from .jobs import JobQueue
from .folder_picker import select_photo_folders
from .folder_tree import FolderTree
from .library import _identifier, natural_key, scan_folder
from .matching import close_video_probe, resolve_pair
from .models import MediaItem
from .multi_cache import normalise_roots, open_multi_cache, scan_roots
from .reveal_file import reveal_in_folder
from .settings import Preferences, app_directory, cache_data_roots, data_directory
from .settings_controls import PillSwitch, SettingsScrollArea, SettingsWheelGuard, TickCheckBox
from .theme import DARK_STYLE, stylesheet_for
from .ui_theme import application_theme, bind_theme, tokens_for
from .system_open import enumerate_apps, open_with
from .touch_grid import OverviewGrid
from .touch_filmstrip import FilmstripResizeHandle, TouchFilmstrip
from .viewer import ViewerCanvas


STYLE = DARK_STYLE


def _pil_to_qimage(image) -> QImage:
    """Copy pixel storage before the worker's Pillow image is released."""
    rgb = image.convert("RGB")
    return QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3,
                  QImage.Format.Format_RGB888).copy()


def _decode(path: Path, edge: int | None):
    image = load_image(path, edge)
    return _pil_to_qimage(image), image.info.get("original_size", image.size)


class _ScanSignals(QObject):
    progress = Signal(int, int)
    complete = Signal(int, object, object)


class _RevealSignals(QObject):
    finished = Signal(str)


class _CacheSignals(QObject):
    progress = Signal(int, int, int)
    verified = Signal(int, object, object)


@dataclass
class _CacheLoad:
    cache: object
    key: tuple
    items: object
    warning: str = ''
    location: str = 'user'
    owned: bool = True
    bound_roots: tuple = ()
    missing_roots: tuple = ()


class _FullscreenLiveBadge(QLabel):
    """A free-standing overlay has no layout to apply changed size hints."""

    def fit_contents(self):
        if getattr(self, '_fitting_contents', False):
            return
        self._fitting_contents = True
        try:
            self.ensurePolished()
            hint = self.sizeHint().expandedTo(self.minimumSizeHint())
            if hint.isValid() and self.size() != hint:
                self.resize(hint)
        finally:
            self._fitting_contents = False

    def event(self, event):
        handled = super().event(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.FontChange,
                            QEvent.Type.ApplicationFontChange, QEvent.Type.Polish,
                            QEvent.Type.DevicePixelRatioChange, QEvent.Type.ScreenChangeInternal,
                            QEvent.Type.Show):
            self.fit_contents()
        return handled


class FilenameField(QLineEdit):
    """Selectable filename text with native copy controls and no rename action."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setFrame(False)
        self.setTextMargins(0, 0, 0, 0)
        self.setCursorPosition(0)

    def setText(self, text):
        # Preview/pair updates can reapply the same name while the user copies
        # it. Leave selection, cursor and horizontal scroll untouched then.
        if text == self.text():
            return
        super().setText(text)
        self.setCursorPosition(0)


class MediaTypeFilter(QPushButton):
    """A dropdown whose independent checkboxes remain open while selecting."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.menu = QMenu(self)
        self.live = self._add_option("Live Photos", True)
        self.static = self._add_option("静态照片", True)
        self.menu.addSeparator()
        self.pending = self._add_option("只显示待检查项目", False)
        self.setMenu(self.menu)
        self.setMinimumWidth(165)
        self._update_label()

    def _add_option(self, text, checked):
        box = TickCheckBox(text)
        box.setChecked(checked)
        box.setStyleSheet(self._option_style())
        action = QWidgetAction(self.menu)
        action.setDefaultWidget(box)
        self.menu.addAction(action)
        box.toggled.connect(self._selection_changed)
        return box

    def _option_style(self):
        colors = tokens_for(self)
        return f"""
            QCheckBox {{ padding: 7px 15px; spacing: 10px; background: transparent; }}
            QCheckBox:hover {{ background: {colors['hover']}; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid {colors['muted']};
                                   border-radius: 3px; background: transparent; }}
            QCheckBox::indicator:checked {{ border-color: {colors['switch_on']}; background: {colors['switch_on']}; }}
        """

    def apply_theme(self):
        for box in (self.live, self.static, self.pending):
            box.setStyleSheet(self._option_style())

    def _selection_changed(self):
        self._update_label()
        self.changed.emit()

    def _update_label(self):
        selected = []
        if self.live.isChecked():
            selected.append("Live Photos")
        if self.static.isChecked():
            selected.append("静态照片")
        title = "全部图片" if len(selected) == 2 else " / ".join(selected) or "未选择图片类型"
        if self.pending.isChecked():
            title += " · 待检查"
        self.setText(title)


APP_SHORTCUTS = {
    "previous": ("Left", "上一张"),
    "next": ("Right", "下一张"),
    "fullscreen": ("F11", "切换全屏"),
    "open_folder": ("Ctrl+O", "打开文件夹"),
    "select_folders": ("Ctrl+A", "全选侧栏文件夹"),
}


def _matches_shortcut(event, name: str) -> bool:
    return event.keyCombination() == QKeySequence(APP_SHORTCUTS[name][0])[0]


def _overview_zoom_direction(key, modifiers):
    modifiers = modifiers & ~Qt.KeyboardModifier.KeypadModifier
    control = Qt.KeyboardModifier.ControlModifier
    if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal) and modifiers in (control, control | Qt.KeyboardModifier.ShiftModifier):
        return 1
    if key == Qt.Key.Key_Minus and modifiers == control:
        return -1
    return 0


def _shortcut_error(hold: QKeySequence, once: QKeySequence, filmstrip: QKeySequence) -> str:
    sequences = (("按住播放", hold), ("单击播放", once), ("切换胶片条", filmstrip))
    if any(sequence.isEmpty() for _label, sequence in sequences):
        return "请分别设置按住播放、单击播放、切换胶片条的按键或组合键。"
    for _label, sequence in sequences:
        if sequence[0].key() == Qt.Key.Key_Escape:
            return "Esc 保留用于退出全屏和返回总览，不能设为自定义快捷键。"
    for index, (label, sequence) in enumerate(sequences):
        for other_label, other_sequence in sequences[index + 1:]:
            if sequence == other_sequence:
                return f"{label}与{other_label}不能使用相同的快捷键，请修改其中一个。"
    return ""


def _shortcut_overrides(hold: QKeySequence, once: QKeySequence, filmstrip: QKeySequence) -> list[str]:
    overrides = []
    for label, sequence in (("按住播放", hold), ("单击播放", once), ("切换胶片条", filmstrip)):
        for key, operation in APP_SHORTCUTS.values():
            if sequence == QKeySequence(key):
                if key == "F11":
                    overrides.append(f"F11：窗口模式改为“{label}”；全屏时仍用于退出全屏")
                elif key == 'Ctrl+A':
                    overrides.append(f"Ctrl+A：侧栏仍用于全选文件夹；其他区域改为“{label}”")
                else:
                    overrides.append(f"{sequence.toString(QKeySequence.SequenceFormat.NativeText)}：由“{operation}”改为“{label}”")
        if not sequence.isEmpty():
            combination = sequence[0]
            direction = _overview_zoom_direction(combination.key(), combination.keyboardModifiers())
            if direction:
                operation = '放大总览缩略图' if direction > 0 else '缩小总览缩略图'
                overrides.append(f"{sequence.toString(QKeySequence.SequenceFormat.NativeText)}：由“{operation}”改为“{label}”")
    return overrides


class SettingsDialog(QDialog):
    def __init__(self, preferences: Preferences, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LiveViewer 设置")
        self.setMinimumWidth(530)
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 20)
        title = QLabel("按你的习惯浏览")
        title.setStyleSheet("font-size:21px;font-weight:600;padding-bottom:6px")
        outer.addWidget(title)
        scroll = self.scroll_area = SettingsScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        sections = QVBoxLayout(scroll_content)
        sections.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(scroll_content)
        outer.addWidget(scroll, 1)
        appearance = QGroupBox('外观')
        appearance_form = QFormLayout(appearance)
        self.theme_mode = QComboBox()
        for label, mode in (('深色', 'dark'), ('浅色', 'light'), ('跟随系统', 'system')):
            self.theme_mode.addItem(label, mode)
        self.theme_mode.setCurrentIndex(max(0, self.theme_mode.findData(preferences.theme_mode)))
        appearance_form.addRow('界面主题', self.theme_mode)
        theme_help = QLabel('跟随系统时，自动响应 Windows 的应用外观变化。')
        theme_help.setObjectName('muted')
        theme_help.setWordWrap(True)
        appearance_form.addRow(theme_help)
        sections.addWidget(appearance)
        play_group = QGroupBox("Live Photo 播放")
        form = QFormLayout(play_group)
        form.setVerticalSpacing(13)
        self.mode = QComboBox()
        self.mode.addItems(["播放一次，结束后恢复照片", "持续按住时循环播放"])
        self.mode.setCurrentIndex(1 if preferences.loop else 0)
        form.addRow("播放模式", self.mode)
        self.autoplay = PillSwitch("切换到 Live Photo 时自动播放一次")
        self.autoplay.setChecked(preferences.autoplay)
        form.addRow("自动播放", self.autoplay)
        autoplay_help = QLabel("下次切换照片起生效；播完恢复静态照片，不受按住时循环设置影响。总览或对话框中不会自动播放。")
        autoplay_help.setWordWrap(True)
        autoplay_help.setObjectName("muted")
        form.addRow(autoplay_help)
        self.muted = PillSwitch("默认静音")
        self.muted.setChecked(preferences.muted)
        form.addRow("声音", self.muted)
        self.zoom_mode = QComboBox()
        self.zoom_mode.addItems(["沿用照片当前放大区域", "临时完整播放，停止后恢复原视图"])
        self.zoom_mode.setCurrentIndex(0 if preferences.preserve_zoom else 1)
        form.addRow("放大时播放", self.zoom_mode)
        self.horizontal_scroll_mode = QComboBox()
        self.horizontal_scroll_mode.addItems(["切换上一张／下一张", "放大后左右移动画面"])
        self.horizontal_scroll_mode.setCurrentIndex(1 if preferences.horizontal_scroll_mode == "pan" else 0)
        form.addRow("拇指／水平滚轮", self.horizontal_scroll_mode)
        horizontal_help = QLabel("设置水平滚轮的用途：切换照片，或在放大后左右移动画面。部分鼠标需要在自带软件中将拇指滚轮设为“水平滚动”。垂直滚轮仍用于缩放图片。")
        horizontal_help.setWordWrap(True)
        horizontal_help.setObjectName("muted")
        form.addRow(horizontal_help)
        self.hold = QSpinBox()
        self.hold.setRange(150, 1000)
        self.hold.setSingleStep(50)
        self.hold.setSuffix(" 毫秒")
        self.hold.setValue(preferences.hold_ms)
        form.addRow("鼠标／触屏长按", self.hold)
        self.live_key = QKeySequenceEdit(QKeySequence(preferences.live_key))
        self.live_key.setMaximumSequenceLength(1)
        self.live_key.setFinishingKeyCombinations([])
        form.addRow("按住播放快捷键", self.live_key)
        self.live_once_key = QKeySequenceEdit(QKeySequence(preferences.live_once_key))
        self.live_once_key.setMaximumSequenceLength(1)
        self.live_once_key.setFinishingKeyCombinations([])
        form.addRow("单击播放到结束快捷键", self.live_once_key)
        self.filmstrip_key = QKeySequenceEdit(QKeySequence(preferences.filmstrip_key))
        self.filmstrip_key.setMaximumSequenceLength(1)
        self.filmstrip_key.setFinishingKeyCombinations([])
        form.addRow("显示／隐藏胶片条快捷键", self.filmstrip_key)
        help_text = QLabel("按住播放：松开立即停止。单击播放：松开后继续，播完一次恢复照片。\n胶片条快捷键在全屏同样可用，总览中记住选择。可设置单键或组合键；输入文字时不触发。Esc 保留用于退出全屏和返回总览；全屏时 F11 始终退出全屏。")
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        form.addRow(help_text)
        self.shortcut_note = QLabel()
        self.shortcut_note.setWordWrap(True)
        form.addRow(self.shortcut_note)
        self.override_keys = PillSwitch("确认替换以上原有快捷键")
        form.addRow(self.override_keys)
        self.live_key.keySequenceChanged.connect(self._shortcuts_changed)
        self.live_once_key.keySequenceChanged.connect(self._shortcuts_changed)
        self.filmstrip_key.keySequenceChanged.connect(self._shortcuts_changed)
        self._shortcuts_changed()
        sections.addWidget(play_group)
        storage_group = QGroupBox("文件夹与缓存")
        storage = QFormLayout(storage_group)
        storage.setVerticalSpacing(12)
        self.recursive = PillSwitch("包含所有子文件夹（只在同一目录内配对）")
        self.recursive.setChecked(preferences.recursive)
        storage.addRow(self.recursive)
        self.cache_location = QComboBox()
        self.cache_location.addItem("本机用户目录", "user")
        self.cache_location.addItem("软件目录内 data/cache", "program")
        self.cache_location.addItem("照片文件夹内 LiveViewer Cache（隐藏）", "library")
        self.cache_location.setCurrentIndex(max(0, self.cache_location.findData(preferences.cache_location)))
        storage.addRow("缓存保存位置", self.cache_location)
        cache_note = QLabel("照片目录缓存随图库移动，自动复用未变化的项目；目录不可写或正在使用时会使用本机缓存。")
        cache_note.setWordWrap(True)
        cache_note.setObjectName("muted")
        storage.addRow(cache_note)
        self.cache = QSpinBox()
        self.cache.setRange(256, 16384)
        self.cache.setSingleStep(256)
        self.cache.setSuffix(" MB")
        self.cache.setValue(preferences.cache_mb)
        storage.addRow("缩略图缓存上限", self.cache)
        clear_cache = QPushButton("清理缩略图缓存")
        clear_cache.setToolTip("只清理生成的缩略图；保留配对结果和手动选择，不修改源文件。")
        if parent is not None:
            clear_cache.clicked.connect(parent._clear_cache)
        storage.addRow(clear_cache)
        self.portable = PillSwitch("设置跟随软件目录（完全便携设置）")
        self.portable.setChecked((app_directory() / "portable.flag").exists())
        storage.addRow(self.portable)
        portable_note = QLabel("设置保存位置切换后重启生效；缓存位置可独立选择并立即切换。选择图库缓存或保存封面图时会写入照片目录。")
        portable_note.setWordWrap(True)
        portable_note.setObjectName("muted")
        storage.addRow(portable_note)
        sections.addWidget(storage_group)
        sections.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._wheel_guard = SettingsWheelGuard(scroll)
        self._wheel_guard.protect(self.theme_mode, self.mode, self.zoom_mode, self.horizontal_scroll_mode,
                                  self.cache_location, self.hold, self.cache)
        screen = self.screen()
        available_height = screen.availableGeometry().height() if screen else 900
        self.resize(620, min(850, max(420, available_height - 70)))
        bind_theme(self)

    def _accept(self):
        sequences = (self.live_key.keySequence(), self.live_once_key.keySequence(), self.filmstrip_key.keySequence())
        error = _shortcut_error(*sequences)
        if error:
            QMessageBox.information(self, "播放快捷键", error)
            return
        overrides = _shortcut_overrides(*sequences)
        if overrides and not self.override_keys.isChecked():
            QMessageBox.information(self, "快捷键冲突", "\n".join(overrides) + "\n\n请开启确认替换，或更换播放快捷键。")
            return
        self.accept()

    def _shortcuts_changed(self, *_args):
        sequences = (self.live_key.keySequence(), self.live_once_key.keySequence(), self.filmstrip_key.keySequence())
        error = _shortcut_error(*sequences)
        overrides = _shortcut_overrides(*sequences)
        self.shortcut_note.setText(error or "\n".join(overrides))
        self.shortcut_note.setVisible(bool(error or overrides))
        self.override_keys.setChecked(False)
        self.override_keys.setVisible(bool(overrides) and not error)


class MainWindow(QMainWindow):
    """Read-only local media browser; all expensive reads run off the UI thread."""

    image_ready = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LiveViewer · 本地 Live Photo 浏览器")
        self.resize(1420, 930)
        self.setMinimumSize(940, 650)
        self.preferences = Preferences.load()
        self.theme_controller = application_theme(self.preferences.theme_mode)
        self.setStyleSheet(stylesheet_for(self.theme_controller.effective_theme))
        self._data_base = data_directory()
        self._user_cache_base, self._program_cache_base = cache_data_roots()
        bootstrap = 'program' if self.preferences.cache_location == 'program' else 'user'
        bootstrap_base = self._program_cache_base if bootstrap == 'program' else self._user_cache_base
        # Retain the existing small global-cache bootstrap. All photo-root
        # creation, location switching and imports below run on worker threads.
        bootstrap_warning = ''
        try:
            self.cache = CacheStore(bootstrap_base / "cache", self.preferences.cache_mb * 1024 ** 2,
                                    legacy_sources=tuple(path / 'cache' for path in
                                                         (self._user_cache_base, self._program_cache_base)
                                                         if path != bootstrap_base))
        except (OSError, ValueError, sqlite3.Error):
            if bootstrap != 'program':
                raise
            self.cache = CacheStore(self._user_cache_base / 'cache', self.preferences.cache_mb * 1024 ** 2)
            bootstrap = 'user'
            bootstrap_warning = '软件目录缓存暂不可写，已使用本机缓存。'
        self._cache_key = (bootstrap, None)
        self._cache_bound_roots = ()
        self._cache_warning = bootstrap_warning or self.cache.recovery_warning
        self._cache_location = bootstrap
        self._cache_lock = threading.RLock()
        self._cache_prepare_lock = threading.Lock()
        self._cache_stores = {id(self.cache): self.cache}
        self._cache_close_locks = {}
        self._cache_reapers = []
        self._load_threads = []
        self._verify_threads = {}
        self._verify_cancel = threading.Event()
        self._verify_generation = 0
        self._verify_running = False
        self.jobs = JobQueue(self)
        self.jobs.finished.connect(self._job_finished)
        self.jobs.discarded.connect(self._job_discarded)
        self._scan_signals = _ScanSignals(self)
        self._scan_signals.progress.connect(self._scan_progress)
        self._scan_signals.complete.connect(self._scan_complete)
        self._cache_signals = _CacheSignals(self)
        self._cache_signals.progress.connect(self._verify_progress)
        self._cache_signals.verified.connect(self._verify_complete)
        self._reveal_signals = _RevealSignals(self)
        self._reveal_signals.finished.connect(self._reveal_finished)
        self._reveal_pending = False
        self._reveal_thread = None
        self._scan_cancel = threading.Event()
        self._scan_generation = 0
        self._epoch = 0
        self._closed = False
        self._shutdown_complete = threading.Event()
        self._shutdown_error = None
        self._shutdown_thread = None
        self.folder: Path | None = None
        self.folders: tuple[Path, ...] = ()
        self._scan_pending = False
        self._primary_items: list[MediaItem] = []
        self._extras: dict[str, list[MediaItem]] = {}
        self._items: list[MediaItem] = []
        self._directory_filter: Path | None = None
        self._current_id: str | None = None
        self._displayed_item: MediaItem | None = None
        self._awaiting_pair: str | None = None
        self._request_context: dict[str, tuple] = {}
        self._pair_revisions: dict[str, int] = {}
        self._previews = OrderedDict()
        self._preview_bytes = 0
        self._original_size = (0, 0)
        self._current_full = False
        self._full_requested = False
        self._deferred_upgrade = None
        self._deferred_candidate = None
        self._shown_image_path = None
        self._selection_generation = 0
        self._autoplay_armed = False
        self._autoplay_consumed = -1
        self._navigation_target_id = None
        self._file_times: dict[str, float] = {}
        self._grid_mode = False
        self._is_playing = False
        self._keyboard_live_pressed = False
        self._keyboard_live_key = None
        self._chrome_visible = True
        self._fullscreen_active = False
        self._pre_fullscreen_geometry = None
        self._pre_fullscreen_maximized = False
        self._disk_missing = False
        self._missing_folders = frozenset()
        self._background_pair_id = None
        self._remembered_strip = self.preferences.filmstrip_visible
        self._live_sequence = QKeySequence(self.preferences.live_key)
        self._once_sequence = QKeySequence(self.preferences.live_once_key)
        self._filmstrip_sequence = QKeySequence(self.preferences.filmstrip_key)
        self._build_ui()
        self.viewer.set_options(self.preferences.loop, self.preferences.muted,
                                self.preferences.preserve_zoom, self.preferences.hold_ms,
                                self.preferences.horizontal_scroll_mode)
        self.viewer.navigate_requested.connect(self._navigate)
        self.viewer.zoom_changed.connect(self._zoom_changed)
        self.viewer.playback_changed.connect(self._playback_changed)
        self.viewer.message.connect(self._show_message)
        self.viewer.controls_toggled.connect(self._toggle_controls)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)
        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(5000)
        self._disk_timer.timeout.connect(self._check_disk)
        self._disk_timer.start()
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setSingleShot(True)
        self._analysis_timer.timeout.connect(self._schedule_background_pair)
        self._navigation_timer = QTimer(self)
        self._navigation_timer.setSingleShot(True)
        self._navigation_timer.setInterval(45)
        self._navigation_timer.timeout.connect(self._flush_navigation)
        self._grid_size_save_timer = QTimer(self)
        self._grid_size_save_timer.setSingleShot(True)
        self._grid_size_save_timer.setInterval(250)
        self._grid_size_save_timer.timeout.connect(self._save_preferences)
        self.grid.thumbnail_size_changed.connect(self._overview_size_changed)
        QApplication.instance().installEventFilter(self)
        self._update_hint()
        self.statusBar().showMessage("本地浏览 · JPG / JPEG / HEIC + MOV · 支持触屏与普通图片")
        bind_theme(self, self._apply_theme)

    def _apply_theme(self, _scheme):
        colors = tokens_for(self)
        self.kind_filter.apply_theme()
        self.viewer.apply_theme(colors)
        self.fullscreen_live_badge.setStyleSheet(
            f"QLabel {{ color: {colors['badge_text']}; background: {colors['badge_background']}; "
            f"border: 1px solid {colors['badge_border']}; border-radius: 5px; padding: 5px 8px; font-weight: 600; }}")
        self.fullscreen_live_badge.fit_contents()
        self.grid.viewport().update()
        self.strip.viewport().update()
        self.film_resize_handle.update()

    @staticmethod
    def _button(text, callback=None, *, checkable=False, name=None):
        button = QPushButton(text)
        button.setCheckable(checkable)
        if name:
            button.setObjectName(name)
        if callback:
            button.clicked.connect(callback)
        return button

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = QFrame()
        self.header.setObjectName("header")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(22, 14, 20, 14)
        header.setSpacing(10)
        brand_column = QVBoxLayout()
        brand_column.setSpacing(0)
        brand = QLabel("LiveViewer")
        brand.setObjectName("brand")
        brand_column.addWidget(brand)
        subtitle = QLabel("让照片中的瞬间，再动起来")
        subtitle.setObjectName("subtitle")
        brand_column.addWidget(subtitle)
        header.addLayout(brand_column)
        header.addStretch()
        self.open_button = self._button("＋  打开文件夹", self.choose_folder, name="primary")
        header.addWidget(self.open_button)
        self.sidebar_button = self._button("文件夹", self._toggle_sidebar, checkable=True)
        self.sidebar_button.setChecked(True)
        header.addWidget(self.sidebar_button)
        self.grid_button = self._button("▦  总览", self._toggle_grid, checkable=True)
        header.addWidget(self.grid_button)
        self.strip_button = self._button("胶片条", self._toggle_strip, checkable=True)
        self.strip_button.setChecked(self._remembered_strip)
        header.addWidget(self.strip_button)
        self.fullscreen_button = self._button("⛶  全屏", self._toggle_fullscreen)
        self.fullscreen_button.setToolTip("F11 进入／退出全屏；Esc 退出全屏")
        header.addWidget(self.fullscreen_button)
        header.addWidget(self._button("设置", self.open_settings))
        root.addWidget(self.header)

        self.filterbar = QFrame()
        self.filterbar.setObjectName("filterbar")
        filters = QHBoxLayout(self.filterbar)
        filters.setContentsMargins(20, 10, 20, 10)
        filters.setSpacing(12)
        self.path_label = QLabel("尚未选择文件夹")
        self.path_label.setObjectName("muted")
        self.path_label.setMinimumWidth(120)
        self.path_label.setMaximumWidth(430)
        filters.addWidget(self.path_label, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件名或路径…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self._apply_filters)
        filters.addWidget(self.search, 1)
        self.search_scope = QComboBox()
        self.search_scope.setAccessibleName('搜索范围')
        self.search_scope.addItem('已选文件夹', 'current')
        self.search_scope.addItem('全局', 'all')
        self.search_scope.setCurrentIndex(max(0, self.search_scope.findData(self.preferences.search_scope)))
        self.search_scope.setMaximumWidth(145)
        self.search_scope.setToolTip('已选文件夹：只搜索侧栏勾选的文件夹。全局：搜索全部已导入图片。清空搜索后恢复勾选范围。')
        self.search_scope.currentIndexChanged.connect(self._search_scope_changed)
        filters.addWidget(self.search_scope)
        self.kind_filter = MediaTypeFilter()
        self.kind_filter.changed.connect(self._apply_filters)
        filters.addWidget(self.kind_filter)
        self.sort = QComboBox()
        self.sort.addItems(["文件名排序", "文件时间排序"])
        self.sort.setMaximumWidth(150)
        self.sort.setCurrentIndex(1 if self.preferences.sort_by == "time" else 0)
        self.sort.currentIndexChanged.connect(self._sort_changed)
        filters.addWidget(self.sort)
        self.rescan_button = self._button("↻", self._rescan)
        self.rescan_button.setFixedWidth(42)
        self.rescan_button.setToolTip("重新扫描文件夹；图片和视频不会被修改")
        filters.addWidget(self.rescan_button)
        root.addWidget(self.filterbar)
        self.toast = QLabel()
        self.toast.setObjectName("toast")
        self.toast.setWordWrap(True)
        self.toast.hide()
        root.addWidget(self.toast)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(12, 17, 8, 12)
        title = QLabel("文件夹")
        title.setObjectName('muted')
        title.setStyleSheet("font-size:12px;font-weight:600;padding-left:5px")
        side.addWidget(title)
        self.tree = FolderTree()
        self.tree.checkedChanged.connect(self._directories_checked)
        side.addWidget(self.tree)
        side_note = QLabel("勾选多选 · Shift 选范围 · Ctrl+A 全选\n图片与视频仅在保存封面图时更改\n缓存按设置位置存放")
        side_note.setObjectName("muted")
        side_note.setWordWrap(True)
        side_note.setStyleSheet("font-size:11px;padding:5px")
        side.addWidget(side_note)
        self.sidebar.setMinimumWidth(170)
        self.sidebar.setMaximumWidth(370)
        splitter.addWidget(self.sidebar)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.infobar = QFrame()
        self.infobar.setObjectName("infobar")
        info = QHBoxLayout(self.infobar)
        info.setContentsMargins(20, 10, 18, 10)
        self.filename_label = FilenameField("你的本地照片库")
        self.filename_label.setObjectName("filename")
        self.filename_label.setMinimumWidth(80)
        self.filename_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(self.filename_label, 1)
        self.live_badge = QLabel("◉ LIVE")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.hide()
        info.addWidget(self.live_badge)
        self.counter_label = QLabel("0 / 0")
        self.counter_label.setObjectName("muted")
        info.addWidget(self.counter_label)
        self.previous_button = self._button("‹", lambda: self._navigate(-1))
        self.previous_button.setToolTip("上一张 · ←")
        self.next_button = self._button("›", lambda: self._navigate(1))
        self.next_button.setToolTip("下一张 · →")
        info.addWidget(self.previous_button)
        info.addWidget(self.next_button)
        self.more_button = self._button("•••", self._show_item_menu)
        info.addWidget(self.more_button)
        right_layout.addWidget(self.infobar)

        self.model = CatalogModel(self)
        self.model.thumbnail_requested.connect(self._request_thumbnail)
        self.content = QStackedWidget()
        self.empty_page = QWidget()
        empty = QVBoxLayout(self.empty_page)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setSpacing(14)
        self.empty_icon = QLabel("◉")
        self.empty_icon.setObjectName("emptyIcon")
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_icon)
        self.empty_title = QLabel("从一个文件夹开始")
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_title)
        self.empty_body = QLabel("静止时，欣赏照片。按住时，重温瞬间。\n支持 JPG / JPEG / HEIC + MOV，以及常用图片格式。")
        self.empty_body.setObjectName("emptyBody")
        self.empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_body.setWordWrap(True)
        self.empty_body.setMaximumWidth(620)
        empty.addWidget(self.empty_body)
        self.empty_open = self._button("选择照片文件夹", self.choose_folder, name="primary")
        empty.addWidget(self.empty_open, alignment=Qt.AlignmentFlag.AlignCenter)
        self.content.addWidget(self.empty_page)
        self.viewer = ViewerCanvas()
        self.viewer.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.viewer.customContextMenuRequested.connect(self._viewer_context_menu)
        self.content.addWidget(self.viewer)
        self.grid = OverviewGrid()
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(ThumbnailDelegate(self.grid, large=True))
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(5)
        self.grid.set_thumbnail_width(self.preferences.overview_thumbnail_width)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self._list_context_menu(self.grid, pos))
        self.grid.clicked.connect(self._grid_selected)
        self.content.addWidget(self.grid)
        right_layout.addWidget(self.content, 1)

        self.viewer_tools = QFrame()
        tools = QHBoxLayout(self.viewer_tools)
        tools.setContentsMargins(18, 7, 18, 7)
        self.hint_label = QLabel()
        self.hint_label.setObjectName("muted")
        tools.addWidget(self.hint_label, 1)
        self.sound_button = self._button("静音", self._toggle_sound, checkable=True)
        self.sound_button.setChecked(self.preferences.muted)
        tools.addWidget(self.sound_button)
        tools.addWidget(self._button("－", self.viewer.zoom_out))
        self.zoom_label = QLabel("适应窗口")
        self.zoom_label.setMinimumWidth(72)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tools.addWidget(self.zoom_label)
        tools.addWidget(self._button("＋", self.viewer.zoom_in))
        tools.addWidget(self._button("适应", self.viewer.fit_image))
        right_layout.addWidget(self.viewer_tools)

        self.film = QFrame()
        self.film.setObjectName("film")
        film_layout = QVBoxLayout(self.film)
        film_layout.setContentsMargins(12, 0, 12, 5)
        film_layout.setSpacing(2)
        self.film_resize_handle = FilmstripResizeHandle(self.film)
        self.film_resize_handle.heightRequested.connect(self._set_film_height)
        film_layout.addWidget(self.film_resize_handle)
        collapse_row = QHBoxLayout()
        collapse_row.addStretch()
        self.collapse_strip_button = self._button("⌄", self._hide_strip)
        self.collapse_strip_button.setObjectName("filmCollapse")
        self.collapse_strip_button.setFixedSize(38, 22)
        self.collapse_strip_button.setStyleSheet("padding:0; min-height:0; font-size:18px; border-radius:5px;")
        self.collapse_strip_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.collapse_strip_button.setToolTip("隐藏胶片条")
        self.collapse_strip_button.setAccessibleName("隐藏胶片条")
        collapse_row.addWidget(self.collapse_strip_button)
        collapse_row.addStretch()
        film_layout.addLayout(collapse_row)
        film_header = QHBoxLayout()
        self.library_label = QLabel("尚未加载照片")
        self.library_label.setObjectName("muted")
        film_header.addWidget(self.library_label)
        film_header.addStretch()
        self.analysis_label = QLabel("按需加载 · 本机缓存")
        self.analysis_label.setObjectName("muted")
        film_header.addWidget(self.analysis_label)
        film_layout.addLayout(film_header)
        self.strip = TouchFilmstrip()
        self.strip.setModel(self.model)
        self.strip.setItemDelegate(ThumbnailDelegate(self.strip))
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setUniformItemSizes(True)
        self.strip.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.strip.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setMinimumHeight(80)
        self.strip.viewportHeightChanged.connect(self._resize_strip_thumbnails)
        self.strip.clicked.connect(lambda index: self._select_row(index.row()))
        self.strip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.strip.customContextMenuRequested.connect(lambda pos: self._list_context_menu(self.strip, pos))
        film_layout.addWidget(self.strip, 1)
        self.film.setFixedHeight(self.preferences.filmstrip_height)
        right_layout.addWidget(self.film)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1200])
        root.addWidget(splitter, 1)
        self.fullscreen_live_badge = _FullscreenLiveBadge("◉ LIVE", central)
        self.fullscreen_live_badge.setObjectName("fullscreenLiveBadge")
        self.fullscreen_live_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.fullscreen_live_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.fullscreen_live_badge.adjustSize()
        self.fullscreen_live_badge.move(16, 16)
        self.fullscreen_live_badge.hide()
        self.content.currentChanged.connect(self._update_live_overlay)
        self.film.setVisible(self._remembered_strip)
        self._update_navigation()

    def choose_folder(self):
        self._navigation_timer.stop()
        self._navigation_target_id = None
        self.viewer.stop_live()
        try:
            selected = select_photo_folders(self, self.folder or Path.home())
        except OSError as error:
            self._show_message('无法打开文件夹选择窗口：' + str(error))
            return
        if selected:
            self.load_folders(selected)

    def _cache_load_key(self, folders):
        location = self.preferences.cache_location
        # Global databases can own read-through handles to a gallery cache.
        # Rebind on every gallery change so those handles retire with that
        # gallery, even when the selected global cache directory is unchanged.
        folders = normalise_roots(folders) if folders else ()
        paths = tuple(str(folder) for folder in folders)
        return location, paths[0] if len(paths) == 1 else paths if paths else None

    def _register_cache(self, cache):
        with self._cache_lock:
            self._cache_stores[id(cache)] = cache

    def _close_cache(self, cache):
        # CacheStore marks itself closed before releasing portable readers and
        # its directory lock. Serialize the whole close, including callbacks.
        with self._cache_lock:
            guard = self._cache_close_locks.setdefault(id(cache), threading.Lock())
        with guard:
            cache.close()
        with self._cache_lock:
            self._cache_stores.pop(id(cache), None)

    @staticmethod
    def _writer_roots(cache):
        if hasattr(cache, 'writer_roots'):
            return cache.writer_roots
        root = cache.library_root
        return (root,) if root is not None and cache.base.parent == root else ()

    def _drain_conflicting_caches(self, roots):
        with self._cache_lock:
            conflicts = [cache for cache in self._cache_stores.values()
                         if set(roots) & set(self._writer_roots(cache))]
        for cache in conflicts:
            self.jobs.wait_owner(cache)
            with self._cache_lock:
                verifiers = [thread for thread, owner in self._verify_threads.items() if owner is cache]
            for verifier in verifiers:
                if verifier is not threading.current_thread():
                    verifier.join()
            self._close_cache(cache)

    def _retire_cache(self, cache):
        """Release inactive database/file locks after their actual users finish."""
        def retire():
            self.jobs.wait_owner(cache)
            with self._cache_lock:
                users = [thread for thread, owner in self._verify_threads.items() if owner is cache]
            for user in users:
                if user is not threading.current_thread():
                    user.join()
            if cache is self.cache and not self._closed:
                return
            self._close_cache(cache)
        thread = threading.Thread(target=retire, name='cache-retire', daemon=True)
        self._cache_reapers.append(thread)
        thread.start()

    def _cancel_verification(self):
        self._verify_cancel.set()
        self._verify_generation += 1
        self._verify_running = False
        dialog = getattr(self, '_verify_dialog', None)
        self._verify_dialog = None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def _prepare_cache_and_scan(self, folders, generation, cancel, *, scan=True):
        folders = normalise_roots(folders) if folders else ()
        key = self._cache_load_key(folders)
        recursive = self.preferences.recursive
        max_bytes = self.preferences.cache_mb * 1024 ** 2

        def prepare():
            owned = False
            prepared = None
            try:
                available = tuple(root for root in folders if root.is_dir())
                missing = tuple(root for root in folders if root not in available)
                if scan and not available:
                    raise NotADirectoryError('没有可访问的已导入文件夹。请检查硬盘连接。')
                with self._cache_prepare_lock:
                    if cancel.is_set() or self._closed:
                        return
                    cache = self.cache
                    prepared, warning, actual = cache, self._cache_warning, self._cache_location
                    if (key != self._cache_key or actual != key[0] or cache._closed
                            or available != self._cache_bound_roots):
                        same_gallery = cache.library_root is None or cache.library_root in available
                        sources = (getattr(cache, 'actual_paths', (cache.base,)) if same_gallery else ())
                        source_paths = tuple(dict.fromkeys((*sources, self._data_base / 'cache',
                                                           self._user_cache_base / 'cache', self._program_cache_base / 'cache')))
                        if key[0] == 'library':
                            self._drain_conflicting_caches(available)
                        if cancel.is_set() or self._closed:
                            return
                        arguments = dict(user_base=self._user_cache_base, program_base=self._program_cache_base,
                                         max_bytes=max_bytes, legacy_sources=source_paths)
                        while True:
                            try:
                                binding = (open_cache(key[0], available[0] if available else None, **arguments)
                                           if len(available) <= 1 else
                                           open_multi_cache(key[0], available, cancel=cancel.is_set, **arguments))
                                break
                            except OSError:
                                remaining = tuple(root for root in available if root.is_dir())
                                if not remaining or remaining == available or cancel.is_set():
                                    raise
                                # A removable disk can disappear during cache
                                # opening. The provider closes a partial bind;
                                # retry only when a root actually went away.
                                available = remaining
                                missing = tuple(root for root in folders if root not in available)
                        prepared, warning, actual = binding.cache, binding.warning, binding.location
                        owned = prepared is not cache
                        self._register_cache(prepared)
                    if cancel.is_set() or self._closed:
                        if owned:
                            self._close_cache(prepared)
                        return
                if cancel.is_set():
                    if owned:
                        self._retire_cache(prepared)
                    return
                items, seen, discovered = ([] if scan else None), set(), 0
                scanned, scan_errors = 0, []
                for folder in scan_roots(available, recursive) if scan else ():
                    if cancel.is_set():
                        break
                    count_at_start = discovered
                    latest_count = [0]
                    def progress(count):
                        latest_count[0] = count
                        self._scan_signals.progress.emit(generation, count_at_start + count)
                    try:
                        indexed = scan_folder(folder, cancel=cancel.is_set, progress=progress, recursive=recursive)
                    except OSError as error:
                        discovered += latest_count[0]
                        scan_errors.append(str(folder) + ': ' + str(error))
                        missing = tuple(dict.fromkeys((*missing, *(root for root in folders
                                                                   if root.is_relative_to(folder)))))
                        continue
                    scanned += 1
                    discovered += latest_count[0]
                    for item in indexed:
                        identity = os.path.normcase(str(item.image_path))
                        if identity not in seen:
                            items.append(item)
                            seen.add(identity)
                if scan and not scanned and scan_errors and not cancel.is_set():
                    raise OSError('\n'.join(scan_errors))
                if missing:
                    warning = '\n'.join(filter(None, (warning, '部分文件夹暂不可访问，已载入其余文件夹：' +
                                                        '、'.join(str(root) for root in missing))))
                packet = _CacheLoad(prepared, key, items, warning, actual, owned, available, missing)
                if cancel.is_set() or self._closed:
                    if owned:
                        self._retire_cache(prepared)
                    return
                self._scan_signals.complete.emit(generation, packet, None)
            except Exception as error:
                if owned and prepared is not None:
                    self._retire_cache(prepared)
                if not cancel.is_set() and not self._closed:
                    self._scan_signals.complete.emit(generation, None, str(error))
        thread = threading.Thread(target=prepare, name='folder-cache-index', daemon=True)
        self._load_threads.append(thread)
        thread.start()

    def _install_cache(self, packet):
        previous = self.cache
        self.cache = packet.cache
        self._cache_key = packet.key
        self._cache_bound_roots = packet.bound_roots
        self._cache_warning = packet.warning
        self._cache_location = packet.location
        self.cache.max_bytes = self.preferences.cache_mb * 1024 ** 2
        self.analysis_label.setText('按需加载 · ' + {'library': '图库缓存', 'program': '软件目录缓存'}.get(packet.location, '本机缓存'))
        if previous is not self.cache:
            self._retire_cache(previous)

    def load_folder(self, folder: Path, *, preserve_view=False):
        self.load_folders((folder,), preserve_view=preserve_view)

    def load_folders(self, folders, *, preserve_view=False):
        folders = normalise_roots(folders)
        if not folders:
            return
        remembered = normalise_roots(self.preferences.last_folders)
        if folders != self.folders and (self.folders or folders != remembered):
            self.preferences.selected_folders = None
        self._restore_grid_after_scan = self._grid_mode if preserve_view else False
        self._scan_allow_autoplay = not preserve_view
        self._cancel_verification()
        self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        cancel = self._scan_cancel
        self._scan_generation += 1
        generation = self._scan_generation
        self._epoch += 1
        self._request_context.clear()
        self.jobs.cancel_where(lambda _key: True)
        self.folders = folders
        self.folder = folders[0]
        self._scan_pending = True
        self._directory_filter = None
        self._primary_items = []
        self._extras.clear()
        self._pair_revisions.clear()
        self._items = []
        self._file_times.clear()
        self._current_id = None
        self._displayed_item = None
        self._awaiting_pair = None
        self._deferred_upgrade = None
        self._deferred_candidate = None
        self._shown_image_path = None
        self._navigation_target_id = None
        self._navigation_timer.stop()
        self._previews.clear()
        self._preview_bytes = 0
        self.viewer.clear()
        self.model.set_items([])
        self.model.pending.clear()
        self.model.failures.clear()
        self.model.thumbnails.clear()
        self.tree.clear()
        self._grid_mode = False
        self.grid_button.setChecked(False)
        self._set_empty("正在扫描文件夹", "先建立文件索引，再按需载入照片与视频。\n可以随时选择其他文件夹取消本次扫描。", "◌")
        self.empty_open.hide()
        self._missing_folders = frozenset()
        self._update_path_label()
        self.filename_label.setText("正在建立照片索引…")
        self.live_badge.hide()
        self.preferences.last_folders = [str(folder) for folder in folders]
        self.preferences.last_folder = str(self.folder)
        self._save_preferences()
        self._disk_missing = False
        self._background_pair_id = None
        self._analysis_timer.stop()
        self._update_navigation()
        self._prepare_cache_and_scan(folders, generation, cancel)

    def _update_path_label(self):
        self.path_label.setText(str(self.folder) if len(self.folders) == 1
                                else f'已打开 {len(self.folders)} 个文件夹')
        paths = '\n'.join(str(folder) for folder in self.folders)
        if self._missing_folders:
            paths += '\n\n暂不可访问：\n' + '\n'.join(map(str, self._missing_folders))
        self.path_label.setToolTip(paths)

    def _scan_progress(self, generation, count):
        if generation == self._scan_generation and not self._closed:
            self.empty_body.setText(f"已发现 {count:,} 张图片，正在扫描目录…\n完整图片和视频不会同时读入内存。")
            self.statusBar().showMessage(f"正在扫描 · 已发现 {count:,} 张图片")

    def _scan_complete(self, generation, items, error):
        if generation != self._scan_generation or self._closed:
            if isinstance(items, _CacheLoad) and items.owned and items.cache is not self.cache:
                self._retire_cache(items.cache)
            return
        self._scan_pending = False
        warning = ''
        if isinstance(items, _CacheLoad):
            self._install_cache(items)
            self._missing_folders = frozenset(items.missing_roots)
            self._disk_missing = bool(self._missing_folders)
            self._update_path_label()
            warning = items.warning
            items = items.items
            if items is None:
                self._show_message(warning or '缓存保存位置已切换。')
                return
        if error:
            self._set_empty("无法打开此文件夹", "请检查文件夹权限或移动硬盘连接。\n" + str(error), "!")
            self.empty_open.show()
            self._show_message("扫描失败：请检查文件夹和硬盘连接。")
            return
        self._primary_items = items
        self._rebuild_items()
        self._build_tree()
        self._apply_filters(select_first=False)
        if not self._items:
            self._set_empty("此文件夹中没有支持的图片", "支持 JPG、JPEG、HEIC、PNG、WebP、BMP、GIF、TIFF 和 AVIF。\n可在设置中开启扫描子文件夹。", "◉")
            self.empty_open.show()
            return
        if not self.model.items:
            self.empty_open.hide()
            return
        preferred = next((row for row, item in enumerate(self.model.items)
                          if str(item.image_path) == self.preferences.last_image), 0)
        self._select_row(preferred, allow_autoplay=getattr(self, '_scan_allow_autoplay', True))
        if getattr(self, '_restore_grid_after_scan', False):
            self._toggle_grid(True)
        self._analysis_timer.start(1500)
        self._show_message(warning or f"已载入 {len(items):,} 个浏览条目；缩略图和同名候选按需分析。")
        if self.preferences.sort_by == "time":
            self._request_times()

    def _rescan(self):
        if self.folders:
            self.load_folders(self.folders)
        else:
            self.choose_folder()

    def _build_tree(self):
        if not self.folders:
            self.tree.clear()
            return
        counts = {}
        for item in self._items:
            parent = item.image_path.parent
            counts[parent] = counts.get(parent, 0) + 1
        self.tree.set_directories(self.folders, counts, self.preferences.selected_folders)

    def _directory_selected(self, tree_item):
        value = tree_item.data(0, Qt.ItemDataRole.UserRole)
        self.tree.set_checked_directories((Path(value),) if value else None)

    def _directories_checked(self, selected):
        self._directory_filter = selected[0] if selected is not None and len(selected) == 1 else None
        self.preferences.selected_folders = None if selected is None else [str(path) for path in selected]
        self._save_preferences()
        self._apply_filters()

    def _search_scope_changed(self, *_args):
        self.preferences.search_scope = self.search_scope.currentData()
        self._save_preferences()
        self._apply_filters()

    def _rebuild_items(self):
        self._items = list(self._primary_items)
        for extras in self._extras.values():
            self._items.extend(extras)

    def _apply_filters(self, *_args, select_first=True):
        if not hasattr(self, "model"):
            return
        needle = self.search.text().strip().casefold()
        show_live = self.kind_filter.live.isChecked()
        show_static = self.kind_filter.static.isChecked()
        pending_only = self.kind_filter.pending.isChecked()
        selected = self.tree.checked_directories if self.folders else None
        global_search = bool(needle) and self.search_scope.currentData() == 'all'
        visible = []
        for item in self._items:
            if not global_search and selected is not None and item.image_path.parent not in selected:
                continue
            if needle and needle not in str(item.image_path).casefold():
                continue
            if item.is_live and not show_live or not item.is_live and not show_static:
                continue
            if pending_only and not (item.pending or item.uncertain):
                continue
            visible.append(item)
        if self.preferences.sort_by == "time":
            visible.sort(key=lambda item: (self._file_times.get(str(item.image_path), float("inf")),
                                           natural_key(item.image_path)))
        else:
            visible.sort(key=lambda item: natural_key(item.image_path))
        old_row = next((row for row, item in enumerate(self.model.items)
                        if item.id == self._current_id), 0)
        grid_scroll = self.grid.verticalScrollBar().value()
        strip_scroll = self.strip.horizontalScrollBar().value()
        self.model.set_items(visible)
        self.grid.verticalScrollBar().setValue(grid_scroll)
        self.strip.horizontalScrollBar().setValue(strip_scroll)
        self.library_label.setText(f"{len(visible):,} / {len(self._items):,} 项 · {sum(i.is_live for i in visible):,} 个 LIVE")
        current_row = next((row for row, item in enumerate(visible) if item.id == self._current_id), None)
        if current_row is not None:
            self._sync_selection(current_row, scroll=False)
        elif visible and (select_first or self._current_id is not None):
            self._select_row(min(old_row, len(visible) - 1))
        elif not visible and self._items:
            self.viewer.stop_live()
            self._current_id = None
            self._displayed_item = None
            self.filename_label.setText("没有符合条件的图片")
            self.live_badge.hide()
            self._set_empty("没有符合条件的图片", "试试清除搜索、切换筛选，或选择其他文件夹。", "⌕")
            self.empty_open.hide()
        self._update_navigation()
        cache_label = {'library': '图库缓存', 'program': '软件目录缓存'}.get(self._cache_location, '本机缓存')
        self.analysis_label.setText(f"待分析 {sum(item.pending for item in self._primary_items):,} 项 · {cache_label}")
        self._update_live_overlay()

    def _sort_changed(self, index):
        self.preferences.sort_by = "time" if index == 1 else "name"
        self._save_preferences()
        if index == 1:
            self._request_times()
        self._apply_filters()

    def _request_times(self):
        if not self._items:
            return
        paths = tuple(item.image_path for item in self._items)
        epoch = self._epoch
        def times():
            result = {}
            for path in paths:
                try:
                    result[str(path)] = path.stat().st_mtime
                except OSError:
                    result[str(path)] = float("inf")
            return result
        self._submit("times", str(epoch), times, 5, (epoch,))

    def _select_row(self, row, *, allow_autoplay=True):
        if not 0 <= row < len(self.model.items):
            return
        item = self.model.items[row]
        self._navigation_timer.stop()
        self._navigation_target_id = None
        self._selection_generation += 1
        self._autoplay_armed = self.preferences.autoplay and allow_autoplay
        self._deferred_candidate = None
        self._shown_image_path = None
        self._prune_media_jobs(item, row)
        self.viewer.clear()
        self._keyboard_live_pressed = False
        self._current_id = item.id
        self._displayed_item = None
        self._current_full = False
        self._full_requested = False
        self._deferred_upgrade = None
        self._is_playing = False
        self._sync_selection(row)
        self.filename_label.setText(item.image_path.name)
        self.filename_label.setToolTip(str(item.image_path))
        self.live_badge.setVisible(item.is_live)
        self.live_badge.setText("◉ LIVE")
        self.preferences.last_image = str(item.image_path)
        self.empty_open.hide()
        self._awaiting_pair = item.id if item.pending else None
        self._load_current(item)
        if item.pending:
            self._request_pair(item, 3)
        self._update_navigation()
        self._update_hint()
        self._prefetch_neighbors(row)
        self._update_live_overlay()

    def _sync_selection(self, row, scroll=True):
        index = self.model.index(row)
        self.strip.setCurrentIndex(index)
        self.grid.setCurrentIndex(index)
        if scroll:
            self.strip.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _load_current(self, item):
        self.filename_label.setText(item.image_path.name)
        self.filename_label.setToolTip(str(item.image_path))
        self._displayed_item = item
        cached = self._previews.get(str(item.image_path))
        if cached:
            self._previews.move_to_end(str(item.image_path))
            self._display_image(item, cached[0], cached[1])
            return
        thumbnail = self.model.thumbnails.get(str(item.image_path))
        if self._shown_image_path is None and thumbnail is not None and not thumbnail.isNull():
            self.viewer.set_photo(thumbnail.toImage(), item.video_path)
            self._shown_image_path = item.image_path
            self.content.setCurrentWidget(self.grid if self._grid_mode else self.viewer)
        elif self._shown_image_path is None:
            self._set_empty("正在载入照片", item.image_path.name + "\n优先载入当前照片，其他照片不会阻塞浏览。", "◌")
        self._submit("image", str(item.image_path), lambda: _decode(item.image_path, 2000), 0,
                     (self._epoch, item.id, item.image_path))

    def _display_image(self, item, image, original_size):
        if self._closed or item.id != self._current_id:
            return
        if self._shown_image_path is not None and (self._is_playing or self.viewer._play_requested):
            self._deferred_candidate = (self._selection_generation, item, image, original_size)
            return
        self._original_size = original_size
        self._current_full = max(original_size) <= max(image.width(), image.height())
        self._displayed_item = item
        if self._shown_image_path is not None:
            self.viewer.replace_photo(image)
        else:
            self.viewer.set_photo(image, item.video_path)
        self._shown_image_path = item.image_path
        self.content.setCurrentWidget(self.grid if self._grid_mode else self.viewer)
        self.filename_label.setText(item.image_path.name)
        self.live_badge.setVisible(item.is_live)
        self.live_badge.setText("! LIVE" if item.uncertain else "◉ LIVE")
        self._update_live_overlay()
        self.preferences.last_image = str(item.image_path)
        self.statusBar().showMessage(item.note or f"{original_size[0]:,} × {original_size[1]:,} · {item.image_path.parent}")
        self._update_hint()
        self.image_ready.emit()
        self._schedule_autoplay()

    def _cache_preview(self, path, image, size):
        key = str(path)
        previous = self._previews.pop(key, None)
        if previous:
            self._preview_bytes -= previous[0].sizeInBytes()
        self._previews[key] = (image, size)
        self._preview_bytes += image.sizeInBytes()
        while len(self._previews) > 8 or self._preview_bytes > 96 * 1024 ** 2:
            if len(self._previews) <= 1:
                break
            _, (old_image, _) = self._previews.popitem(last=False)
            self._preview_bytes -= old_image.sizeInBytes()

    def _request_thumbnail(self, path):
        epoch = self._epoch
        cache = self.cache
        def thumbnail():
            target = cache.get_thumbnail(path)
            return QImage(str(target))
        self._submit("thumb", str(path), thumbnail, 7, (epoch, path), cache=cache)

    def _request_pair(self, item, priority):
        revision = self._pair_revisions.get(item.id, 0)
        cache = self.cache
        self._submit("pair", f"{item.id}:{revision}", lambda: resolve_pair(item, cache), max(3, priority),
                     (self._epoch, item, revision), cache=cache)

    def _schedule_background_pair(self):
        """Gradually expose distinct candidate images without flooding the disk.

        Only one low-priority analysis is outstanding at a time. Current and
        adjacent items may independently request higher-priority analysis.
        Loading/playing a live clip pauses new background submissions.
        """
        if self._closed or self._background_pair_id is not None:
            return
        if self._is_playing or self.viewer._play_requested:
            self._analysis_timer.start(1200)
            return
        pending_ids = {context[2].id for context in self._request_context.values()
                       if context[0] == "pair" and context[1] == self._epoch}
        item = next((item for item in self._primary_items if item.pending and item.id not in pending_ids
                     and not any(item.image_path.is_relative_to(root) for root in self._missing_folders)), None)
        if item is None:
            if any(item.pending for item in self._primary_items):
                self._analysis_timer.start(1200)
            return
        self._background_pair_id = item.id
        self._request_pair(item, 12)

    def _prefetch_neighbors(self, row):
        for offset in (-1, 1):
            neighbor = row + offset
            if not 0 <= neighbor < len(self.model.items):
                continue
            item = self.model.items[neighbor]
            if str(item.image_path) not in self._previews:
                self._submit("image", str(item.image_path), lambda path=item.image_path: _decode(path, 2000), 5,
                             (self._epoch, item.id, item.image_path))

    def _submit(self, kind, identity, function, priority, context, *, cache=None):
        key = f"{kind}:{self._epoch}:{identity}"
        existing = key in self._request_context
        self._request_context[key] = (kind, *context)
        epoch = self._epoch
        def guarded():
            if self._closed or epoch != self._epoch or key not in self._request_context:
                return None
            return function()
        if not self.jobs.submit(key, guarded, priority, owner=cache) and not existing:
            self._job_discarded(key)

    def _job_discarded(self, key):
        context = self._request_context.pop(key, None)
        if context and context[0] == "thumb":
            self.model.pending.discard(str(context[2]))
        if context and context[0] == "pair" and self._background_pair_id == context[2].id:
            self._background_pair_id = None

    def _prune_media_jobs(self, item, row):
        neighbors = self.model.items[max(0, row - 1):row + 2]
        keep_paths = {str(candidate.image_path) for candidate in neighbors}
        current_path = str(item.image_path)
        def obsolete(key):
            context = self._request_context.get(key)
            if context is None:
                return False
            kind = context[0]
            if kind == "image":
                return str(context[3]) != current_path
            if kind == "full":
                return str(context[3]) != current_path
            if kind == "thumb":
                return str(context[2]) not in keep_paths
            if kind == "pair":
                return context[2].id != item.id and context[2].id != self._background_pair_id
            return False
        obsolete_keys = [key for key in self._request_context if obsolete(key)]
        self.jobs.cancel_where(obsolete)
        for key in obsolete_keys:
            self._job_discarded(key)

    def _schedule_autoplay(self):
        generation = self._selection_generation
        if not self._autoplay_armed or self._autoplay_consumed == generation:
            return
        self._autoplay_consumed = generation
        item_id = self._current_id
        def play():
            item = self._displayed_item
            if (self._closed or generation != self._selection_generation or item is None
                    or item.id != item_id or not item.is_live or not self._autoplay_armed
                    or not self.preferences.autoplay or self.viewer._play_requested
                    or self._grid_mode or not self.isVisible() or self.isMinimized() or not self.viewer.isVisible()
                    or self.content.currentWidget() is not self.viewer
                    or QApplication.activeModalWidget() is not None
                    or QApplication.activePopupWidget() is not None):
                return
            self.viewer.play_live_once()
        QTimer.singleShot(0, play)

    def _job_finished(self, key, result, error):
        context = self._request_context.pop(key, None)
        if self._closed or context is None:
            return
        kind, epoch, *data = context
        if epoch != self._epoch:
            return
        if kind == "thumb":
            self.model.put_thumbnail(data[0], None if error else result)
            return
        if kind == "times":
            if not error:
                self._file_times.update(result)
                self._apply_filters(select_first=False)
            return
        if kind == "clear":
            if error:
                self._show_message("缓存清理未完成：" + str(error))
            else:
                self.model.thumbnails.clear()
                self.model.failures.clear()
                self.model.pending.clear()
                self.grid.viewport().update()
                self.strip.viewport().update()
                self._show_message(f"已清理 {result / 1024 ** 2:.1f} MB 缩略图缓存；配对结果和手动选择已保留。")
            return
        if kind == "image":
            item_id, path = data
            current = self._item_by_id(self._current_id)
            applies_to_current = current is not None and current.image_path == path
            if error:
                if applies_to_current:
                    self._set_empty("无法读取这张图片", path.name + "\n请检查文件格式、文件完整性或硬盘连接。", "!")
                    self._show_message(str(error))
                return
            if result is None:
                return
            image, size = result
            self._cache_preview(path, image, size)
            if applies_to_current:
                self._display_image(current, image, size)
            return
        if kind == "full":
            item_id, path = data
            if not self._displayed_item or path != self._displayed_item.image_path:
                return
            self._full_requested = False
            if error:
                self._show_message("原图加载失败，继续使用预览图：" + str(error))
                return
            self._deferred_upgrade = result[0]
            self._apply_deferred_upgrade()
            return
        if kind == "pair":
            original, revision = data
            if self._background_pair_id == original.id:
                self._background_pair_id = None
            self._analysis_timer.start(500)
            if revision != self._pair_revisions.get(original.id, 0):
                return
            current_before = self._current_id
            if error:
                selected = replace(original, pending=False, uncertain=True,
                                   note="自动分析失败，可从更多菜单手动选择静态图片。")
                extras = [MediaItem("extra-" + hashlib.sha256(str(path).encode()).hexdigest()[:22], path,
                                    note="自动分析失败，保留未经确认的同名候选")
                          for path in original.candidates if path != selected.image_path]
            else:
                selected = replace(original, image_path=result.image_path, pending=False,
                                   uncertain=result.uncertain,
                                   note=(original.note + "；" if original.note else "") + result.reason)
                extras = [MediaItem("extra-" + hashlib.sha256(str(path).encode()).hexdigest()[:22], path,
                                    note="同名候选中保留的不同内容图片") for path in result.extra_images]
            self._primary_items = [selected if item.id == original.id else item for item in self._primary_items]
            self._extras[original.id] = extras
            self._rebuild_items()
            self._apply_filters(select_first=False)
            if current_before == original.id and self._current_id == original.id:
                self._awaiting_pair = None
                if self._shown_image_path == selected.image_path:
                    self._displayed_item = selected
                    self.live_badge.setText("! LIVE" if selected.uncertain else "◉ LIVE")
                    self._update_live_overlay()
                else:
                    self.jobs.cancel_where(lambda key: key.startswith(f"full:{self._epoch}:"))
                    self._full_requested = False
                    self._deferred_upgrade = None
                    self._load_current(selected)
            if self.preferences.sort_by == "time" and extras:
                self._request_times()

    def _item_by_id(self, item_id):
        return next((item for item in self._items if item.id == item_id), None)

    def _navigate(self, step):
        if not self.model.items:
            return
        row = next((row for row, item in enumerate(self.model.items) if item.id == self._current_id), 0)
        target = max(0, min(len(self.model.items) - 1, row + step))
        if target != row:
            self._select_row(target)

    def _queue_navigation(self, step):
        if not self.model.items:
            return
        reference = self._navigation_target_id or self._current_id
        row = next((row for row, item in enumerate(self.model.items) if item.id == reference), 0)
        target = max(0, min(len(self.model.items) - 1, row + step))
        self._navigation_target_id = self.model.items[target].id
        self.viewer.stop_live()
        if not self._navigation_timer.isActive():
            self._navigation_timer.start()

    def _flush_navigation(self):
        target, self._navigation_target_id = self._navigation_target_id, None
        self._navigation_timer.stop()
        row = next((row for row, item in enumerate(self.model.items) if item.id == target), None)
        if row is not None and target != self._current_id:
            self._select_row(row)

    def _update_navigation(self):
        row = next((row for row, item in enumerate(self.model.items) if item.id == self._current_id), -1)
        self.counter_label.setText(f"{row + 1:,} / {len(self.model.items):,}")
        self.previous_button.setEnabled(row > 0)
        self.next_button.setEnabled(row >= 0 and row < len(self.model.items) - 1)
        self.more_button.setEnabled(row >= 0)

    def _zoom_changed(self, factor):
        self.zoom_label.setText("适应窗口" if abs(factor - 1) < 0.015 else f"{factor:.2f}×")
        if factor > 1.3 and self._displayed_item and not self._current_full and not self._full_requested:
            width, height = self._original_size
            if width * height > 120_000_000:
                self._show_message("这张图片尺寸过大，继续使用高质量预览以控制内存占用。")
                self._current_full = True
                return
            self._full_requested = True
            item = self._displayed_item
            self._submit("full", str(item.image_path), lambda: _decode(item.image_path, None), 1,
                         (self._epoch, item.id, item.image_path))

    def _apply_deferred_upgrade(self):
        if self._deferred_upgrade is None or self._closed:
            return
        if self._is_playing or self.viewer._play_requested:
            QTimer.singleShot(300, self._apply_deferred_upgrade)
            return
        image, self._deferred_upgrade = self._deferred_upgrade, None
        self._current_full = True
        self.viewer.replace_photo(image)

    def _playback_changed(self, playing):
        self._is_playing = playing
        if playing:
            self.live_badge.setText("▶ LIVE")
        elif self._displayed_item:
            self.live_badge.setText("! LIVE" if self._displayed_item.uncertain else "◉ LIVE")
        if not playing:
            self._apply_deferred_upgrade()
            self._apply_candidate_upgrade()

    def _apply_candidate_upgrade(self):
        if self._deferred_candidate is None or self._is_playing or self.viewer._play_requested:
            return
        generation, item, image, original_size = self._deferred_candidate
        self._deferred_candidate = None
        current = self._item_by_id(self._current_id)
        if (generation == self._selection_generation and current is not None
                and current.id == item.id and current.image_path == item.image_path):
            self._display_image(item, image, original_size)

    def _set_empty(self, title, body, icon="◉"):
        self.empty_title.setText(title)
        self.empty_body.setText(body)
        self.empty_icon.setText(icon)
        if not self._grid_mode:
            self.content.setCurrentWidget(self.empty_page)
        self._update_live_overlay()

    def _show_message(self, text):
        if self._closed:
            return
        self.toast.setText(str(text))
        self.toast.setVisible(not self._fullscreen_active)
        self.statusBar().showMessage(str(text), 8000)
        self._toast_timer.start(6500)

    def _toggle_sidebar(self, checked):
        self.sidebar.setVisible(checked and not self._fullscreen_active)

    def _toggle_strip(self, checked):
        self._remembered_strip = checked
        self.preferences.filmstrip_visible = checked
        self.film.setVisible(checked and not self._grid_mode)
        QTimer.singleShot(0, self._clamp_film_height)
        self._save_preferences()

    def _toggle_strip_shortcut(self):
        self.strip_button.setChecked(not self._remembered_strip)
        self._toggle_strip(self.strip_button.isChecked())

    def _hide_strip(self):
        self.strip_button.setChecked(False)
        self._toggle_strip(False)

    def _toggle_grid(self, checked):
        self.viewer.stop_live()
        self._grid_mode = checked
        self.grid_button.setText("◧  单张" if checked else "▦  总览")
        if checked:
            self.content.setCurrentWidget(self.grid)
            row = next((row for row, item in enumerate(self.model.items) if item.id == self._current_id), 0)
            if self.model.items:
                self.grid.scrollTo(self.model.index(row), QAbstractItemView.ScrollHint.EnsureVisible)
        elif self._displayed_item:
            self.content.setCurrentWidget(self.viewer)
        else:
            self.content.setCurrentWidget(self.empty_page)
        self.film.setVisible(self._remembered_strip and not checked)
        QTimer.singleShot(0, self._clamp_film_height)
        self.viewer_tools.setVisible(not checked and self._chrome_visible)
        self._update_live_overlay()

    def _overview_size_changed(self, width):
        self.preferences.overview_thumbnail_width = width
        self._grid_size_save_timer.start()

    def _set_film_height(self, requested, remember=True):
        available = self.content.height() + (self.film.height() if self.film.isVisible() else 0)
        maximum = max(160, min(480, available - 160))
        height = max(160, min(maximum, int(requested)))
        self.film.setFixedHeight(height)
        if remember:
            self.preferences.filmstrip_height = height
            self._grid_size_save_timer.start()

    def _resize_strip_thumbnails(self, _viewport_height):
        delegate = self.strip.itemDelegate()
        # Reserve the scrollbar even while it is hidden. Its appearance must
        # not change the cell height and trigger an endless relayout cycle.
        scroll_height = self.strip.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        height = max(64, self.strip.height() - 2 * self.strip.frameWidth() - scroll_height - 4)
        if height == delegate.thumbnail_height:
            return
        center = self.strip.viewport().rect().center()
        index = self.strip.indexAt(center)
        if not index.isValid() and self.model.rowCount():
            width = max(1, delegate.sizeHint(None, None).width())
            row = (self.strip.horizontalScrollBar().value() + center.x()) // width
            index = self.model.index(min(self.model.rowCount() - 1, row))
        before = self.strip.visualRect(index).center().x() if index.isValid() else None
        delegate.set_thumbnail_height(height)
        self.strip.doItemsLayout()
        if before is not None:
            delta = self.strip.visualRect(index).center().x() - before
            bar = self.strip.horizontalScrollBar()
            bar.setValue(bar.value() + delta)
        self.strip.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'film'):
            QTimer.singleShot(0, self._clamp_film_height)

    def _clamp_film_height(self):
        if not self._closed and self.film.isVisible():
            self._set_film_height(self.preferences.filmstrip_height, remember=False)

    def _grid_selected(self, index):
        self._grid_mode = False
        self.grid_button.setChecked(False)
        self._toggle_grid(False)
        self._select_row(index.row())

    def _toggle_fullscreen(self):
        """Toggle immediately without ever revealing controls on hover."""
        if self._fullscreen_active:
            self._exit_fullscreen()
            return
        self.viewer.stop_live()
        self._keyboard_live_pressed = False
        self._pre_fullscreen_geometry = self.saveGeometry()
        self._pre_fullscreen_maximized = self.isMaximized()
        self._fullscreen_active = True
        self._toast_timer.stop()
        self._set_chrome(False)
        self.showFullScreen()
        (self.grid if self._grid_mode else self.viewer).setFocus(Qt.FocusReason.OtherFocusReason)

    def _exit_fullscreen(self):
        if not self._fullscreen_active:
            return
        self.viewer.stop_live()
        self._keyboard_live_pressed = False
        self._fullscreen_active = False
        self.showNormal()
        if self._pre_fullscreen_geometry is not None:
            self.restoreGeometry(self._pre_fullscreen_geometry)
        if self._pre_fullscreen_maximized:
            self.showMaximized()
        self._set_chrome(True)

    def _set_chrome(self, visible):
        # Fullscreen is a stable viewing mode, so hover, clicks and delayed
        # status updates can never reveal application controls over the image.
        visible = not self._fullscreen_active
        self._chrome_visible = visible
        self.header.setVisible(visible)
        self.filterbar.setVisible(visible)
        self.infobar.setVisible(visible)
        self.viewer_tools.setVisible(visible and not self._grid_mode)
        self.film.setVisible(self._remembered_strip and not self._grid_mode)
        QTimer.singleShot(0, self._clamp_film_height)
        self.library_label.setVisible(visible)
        self.analysis_label.setVisible(visible)
        self.sidebar.setVisible(visible and self.sidebar_button.isChecked())
        self.statusBar().setVisible(visible)
        if not visible:
            self.toast.hide()
        self._update_live_overlay()

    def _update_live_overlay(self, *_args):
        if not hasattr(self, "fullscreen_live_badge"):
            return
        item = self._displayed_item
        visible = (self._fullscreen_active and not self._grid_mode
                   and self.content.currentWidget() is self.viewer
                   and item is not None and item.id == self._current_id and item.is_live)
        if visible:
            self.fullscreen_live_badge.fit_contents()
        self.fullscreen_live_badge.setVisible(visible)
        if visible:
            self.fullscreen_live_badge.move(16, 16)
            self.fullscreen_live_badge.raise_()

    def _toggle_controls(self):
        if not self._fullscreen_active:
            self.strip_button.setChecked(not self.strip_button.isChecked())
            self._toggle_strip(self.strip_button.isChecked())

    def _toggle_sound(self, muted):
        self.preferences.muted = muted
        self.viewer.set_options(self.preferences.loop, muted,
                                self.preferences.preserve_zoom, self.preferences.hold_ms,
                                self.preferences.horizontal_scroll_mode)
        self._save_preferences()
        self.sound_button.setText("静音" if muted else "声音开启")

    def _update_hint(self):
        key = QKeySequence(self.preferences.live_key).toString(QKeySequence.SequenceFormat.NativeText)
        once = QKeySequence(self.preferences.live_once_key).toString(QKeySequence.SequenceFormat.NativeText)
        self.hint_label.setText(f"按住鼠标／触屏／{key} 播放 · 松开停止 · 按 {once} 播完一次 · 左右滑动切图 · 双指缩放")

    def open_settings(self):
        self._navigation_timer.stop()
        self._navigation_target_id = None
        self.viewer.stop_live()
        dialog = SettingsDialog(self.preferences, self)
        was_portable = (app_directory() / "portable.flag").exists()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.preferences.loop = dialog.mode.currentIndex() == 1
        self.preferences.theme_mode = dialog.theme_mode.currentData()
        self.theme_controller.set_mode(self.preferences.theme_mode)
        self.preferences.autoplay = dialog.autoplay.isChecked()
        self.preferences.muted = dialog.muted.isChecked()
        self.preferences.preserve_zoom = dialog.zoom_mode.currentIndex() == 0
        self.preferences.horizontal_scroll_mode = "pan" if dialog.horizontal_scroll_mode.currentIndex() == 1 else "navigate"
        self.preferences.hold_ms = dialog.hold.value()
        self.preferences.live_key = dialog.live_key.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        self.preferences.live_once_key = dialog.live_once_key.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        self.preferences.filmstrip_key = dialog.filmstrip_key.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        recursive_changed = self.preferences.recursive != dialog.recursive.isChecked()
        self.preferences.recursive = dialog.recursive.isChecked()
        self.preferences.cache_mb = dialog.cache.value()
        cache_changed = self.preferences.cache_location != dialog.cache_location.currentData()
        self.preferences.cache_location = dialog.cache_location.currentData()
        self.cache.max_bytes = self.preferences.cache_mb * 1024 ** 2
        portable_changed = was_portable != dialog.portable.isChecked()
        if portable_changed:
            try:
                flag = app_directory() / "portable.flag"
                if dialog.portable.isChecked():
                    flag.write_text("LiveViewer portable mode\n", encoding="utf-8")
                else:
                    flag.unlink(missing_ok=True)
            except OSError as error:
                self._show_message("无法切换便携模式，请把软件放到可写文件夹：" + str(error))
                portable_changed = False
        self._live_sequence = QKeySequence(self.preferences.live_key)
        self._once_sequence = QKeySequence(self.preferences.live_once_key)
        self._filmstrip_sequence = QKeySequence(self.preferences.filmstrip_key)
        self.viewer.set_options(self.preferences.loop, self.preferences.muted,
                                self.preferences.preserve_zoom, self.preferences.hold_ms,
                                self.preferences.horizontal_scroll_mode)
        self.sound_button.setChecked(self.preferences.muted)
        self.sound_button.setText("静音" if self.preferences.muted else "声音开启")
        self._update_hint()
        self._save_preferences()
        if cache_changed:
            if self.folders:
                self.load_folders(self.folders, preserve_view=True)
            else:
                self._switch_cache_without_folder()
            if portable_changed:
                self._show_message('缓存位置正在切换。设置保存位置已更改，重启后生效。')
        elif portable_changed:
            self._show_message("设置已保存到新模式目录。请重启 LiveViewer；现有缓存保留原位。")
        elif recursive_changed and self.folders:
            self.load_folders(self.folders)
        else:
            self._show_message("设置已保存。")

    def _show_item_menu(self):
        self._item_menu(self.more_button.mapToGlobal(QPoint(0, self.more_button.height())))

    def _viewer_context_menu(self, position):
        self._item_menu(self.viewer.mapToGlobal(position))

    def _list_context_menu(self, view, position):
        index = view.indexAt(position)
        if index.isValid():
            self._select_row(index.row())
            self._item_menu(view.viewport().mapToGlobal(position))

    def _item_menu(self, position):
        item = self._item_by_id(self._current_id)
        if item is None:
            return
        self._navigation_timer.stop()
        self._navigation_target_id = None
        self.viewer.stop_live()
        menu = QMenu(self)
        if item.is_live:
            menu.addAction("更改封面图…", lambda item=item: self._open_cover_editor(item))
        if item.is_live and len(item.candidates) > 1:
            choices = menu.addMenu("选择静态图片")
            for path in item.candidates:
                action = choices.addAction(path.name)
                action.setCheckable(True)
                action.setChecked(path == item.image_path)
                action.triggered.connect(lambda _checked=False, path=path, item=item: self._choose_source(item, path))
            choices.addSeparator()
            choices.addAction("恢复自动择图", lambda item=item: self._choose_source(item, None))
        menu.addAction("文件与配对详情", lambda item=item: self._show_details(item))
        applications = []
        self._add_open_with_menu(menu, item, applications)
        menu.addAction("打开所在文件夹", lambda item=item: self._open_image_folder(item))
        menu.addAction("打开缓存位置", lambda item=item: QDesktopServices.openUrl(QUrl.fromLocalFile(
            str(self.cache.base_for(item) if hasattr(self.cache, 'base_for') else self.cache.base))))
        menu.addAction("深入校验当前图库…", self._verify_cache)
        menu.addSeparator()
        menu.addAction("重新扫描已导入文件夹", self._rescan)
        try:
            menu.exec(position)
        finally:
            for application in applications:
                application.close()
            menu.deleteLater()

    def _add_open_with_menu(self, menu, item, applications):
        submenu = menu.addMenu("以其他软件打开")
        submenu.setToolTipsVisible(True)
        submenu.addAction("正在读取可用应用…").setEnabled(False)
        loaded = [False]
        def populate():
            if loaded[0]:
                return
            loaded[0] = True
            submenu.clear()
            try:
                found = enumerate_apps(item.image_path)
                applications.extend(found)
                for application in found:
                    # Escape ampersands so a friendly Windows name is displayed
                    # literally instead of being interpreted as a Qt mnemonic.
                    action = submenu.addAction(application.display_name.replace("&", "&&"))
                    action.triggered.connect(lambda _checked=False, application=application:
                                             self._invoke_other_app(application, item))
                if not found:
                    submenu.addAction("没有此格式的推荐应用").setEnabled(False)
            except OSError as error:
                failed = submenu.addAction("无法读取应用列表")
                failed.setEnabled(False)
                failed.setToolTip(str(error))
            submenu.addSeparator()
            submenu.addAction("选择其他应用…", lambda: self._open_with_other_app(item))
        submenu.aboutToShow.connect(populate)
        return submenu

    def _open_cover_editor(self, item=None):
        item = item or self._item_by_id(self._current_id)
        if item is None or not item.is_live:
            return
        self._navigation_timer.stop()
        self._navigation_target_id = None
        self.viewer.stop_live()
        self._autoplay_armed = False
        self._keyboard_live_pressed = False
        versions = tuple(file_version(path) for path in (item.candidates or (item.image_path,)))
        dialog = None
        try:
            from .cover_editor import EditorDialog
            dialog = EditorDialog(item, self, initial_export_directory=self.preferences.last_export_directory)
            dialog.frame_exported.connect(self._remember_frame_export)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_result is not None:
                self._apply_cover_result(item, dialog.saved_result, versions)
        except (OSError, ValueError, RuntimeError) as error:
            self._open_error(error)
        finally:
            if dialog is not None:
                # finished already cancels the reader worker. Deferred widget
                # deletion drops its frame/metadata instead of retaining one
                # large child dialog for every edit during this app session.
                dialog.deleteLater()

    def _remember_frame_export(self, path):
        self.preferences.last_export_directory = str(Path(path).parent)
        self._save_preferences()

    def _apply_cover_result(self, original, result, previous_versions=()):
        """Refresh a committed editor result without rescanning the whole disk."""
        affected = {str(path) for path in original.candidates or (original.image_path,)}
        affected.add(str(result.image_path))
        self._pair_revisions[original.id] = self._pair_revisions.get(original.id, 0) + 1
        # A finished worker may already have queued its Qt signal. A new epoch
        # also rejects those late results when the edited path stays unchanged.
        self._epoch += 1
        self.jobs.cancel_where(lambda _key: True)
        self._request_context.clear()
        self._background_pair_id = None
        self.cache.invalidate_item(original, previous_versions)
        for path in affected:
            preview = self._previews.pop(path, None)
            if preview:
                self._preview_bytes -= preview[0].sizeInBytes()
            self.model.thumbnails.pop(path, None)
            self.model.pending.discard(path)
            self.model.failures.discard(path)
            self._file_times.pop(path, None)
        if result.created_copy:
            identifier = _identifier("live", Path(result.image_path).parent / Path(result.image_path).stem.casefold())
            updated = MediaItem(identifier, Path(result.image_path), Path(result.video_path),
                                (Path(result.image_path),), note="已另存新的封面图与 Live Photo 视频")
            self._primary_items.append(updated)
        else:
            updated = replace(original, image_path=Path(result.image_path), video_path=Path(result.video_path),
                              pending=False, uncertain=False, note="已更新封面图")
            self._primary_items = [updated if item.id == original.id else item for item in self._primary_items]
            extras = {str(item.image_path): item for item in self._extras.get(original.id, [])}
            for path in original.candidates:
                if path != updated.image_path:
                    extras.setdefault(str(path), MediaItem("extra-" + hashlib.sha256(str(path).encode()).hexdigest()[:22], path))
            self._extras[original.id] = list(extras.values())
            self.cache.set_override(updated, updated.image_path)
        self._deferred_upgrade = None
        self._deferred_candidate = None
        self._rebuild_items()
        self._apply_filters(select_first=False)
        if not any(item.id == updated.id for item in self.model.items):
            self.search.clear()
            self.kind_filter.live.setChecked(True)
            self.kind_filter.pending.setChecked(False)
            self._apply_filters(select_first=False)
        row = next((row for row, item in enumerate(self.model.items) if item.id == updated.id), None)
        if row is not None:
            self._select_row(row, allow_autoplay=False)
        self._build_tree()
        self._show_message("封面图已另存，已载入新版本。" if result.created_copy else "封面图已更新。")

    def _invoke_other_app(self, application, item):
        self.viewer.stop_live()
        self._keyboard_live_pressed = False
        try:
            application.invoke(item.image_path)
        except OSError as error:
            self._open_error(error)

    def _open_error(self, error):
        if self._fullscreen_active:
            QMessageBox.warning(self, "无法打开", str(error))
        else:
            self._show_message(str(error))

    def _open_image_folder(self, item=None):
        item = item or self._item_by_id(self._current_id)
        if item is None or self._reveal_pending:
            return
        self._navigation_timer.stop()
        self._navigation_target_id = None
        self.viewer.stop_live()
        self._keyboard_live_pressed = False
        self._reveal_pending = True
        path, signals = item.image_path, self._reveal_signals
        def reveal():
            error = ""
            try:
                reveal_in_folder(path)
            except (OSError, ValueError) as failure:
                error = str(failure)
            if not self._closed:
                try:
                    signals.finished.emit(error)
                except RuntimeError:
                    pass  # The user closed the window before Shell returned.
        self._reveal_thread = threading.Thread(target=reveal, name="explorer-reveal", daemon=True)
        self._reveal_thread.start()

    def _reveal_finished(self, error):
        self._reveal_pending = False
        if error and not self._closed:
            self._open_error(error)

    def _open_with_other_app(self, item=None):
        item = item or self._item_by_id(self._current_id)
        if item is None:
            return
        self.viewer.stop_live()
        self._keyboard_live_pressed = False
        try:
            open_with(item.image_path, int(self.winId()))
        except OSError as error:
            self._open_error(error)

    def _choose_source(self, item, path):
        try:
            self.cache.set_override(item, path)
        except (OSError, ValueError) as error:
            self._show_message("无法保存选择：" + str(error))
            return
        updated = replace(item, pending=True)
        self._pair_revisions[item.id] = self._pair_revisions.get(item.id, 0) + 1
        self._primary_items = [updated if existing.id == item.id else existing for existing in self._primary_items]
        self._rebuild_items()
        self._awaiting_pair = item.id
        self._request_pair(updated, 0)
        self._show_message("正在应用静态图片选择…")

    def _clear_cache(self):
        if self._scan_pending or self.cache._closed:
            self._show_message('正在准备文件夹与缓存，请载入后再清理。')
            return
        cache = self.cache
        self._submit("clear", "thumbnails", cache.clear_thumbnails, 2, (self._epoch,), cache=cache)

    def _switch_cache_without_folder(self):
        self._cancel_verification()
        self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        self._scan_generation += 1
        self._epoch += 1
        self._request_context.clear()
        self.jobs.cancel_where(lambda _key: True)
        self._prepare_cache_and_scan((), self._scan_generation, self._scan_cancel, scan=False)

    def _verify_cache(self):
        if not self.folders or self._closed:
            return
        if self._verify_running:
            dialog = getattr(self, '_verify_dialog', None)
            if dialog is not None:
                dialog.show()
                dialog.raise_()
            return
        paths = tuple(dict.fromkeys(path for item in self._primary_items
                                   for path in (*(item.candidates or (item.image_path,)),
                                                *((item.video_path,) if item.video_path else ()))))
        if not paths:
            self._show_message('当前图库没有需要校验的图片或视频。')
            return
        self._verify_generation += 1
        generation = self._verify_generation
        cancel = self._verify_cancel = threading.Event()
        cache, signals = self.cache, self._cache_signals
        self._verify_running = True
        dialog = QProgressDialog('正在完整读取图库文件，照片不会被修改。', '取消校验', 0, len(paths), self)
        dialog.setWindowTitle('深入校验当前图库')
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancel.set)
        self._verify_dialog = dialog
        dialog.show()

        def verify():
            report, error = None, None
            try:
                report = cache.verify_files(paths, cancel=cancel.is_set,
                                            progress=lambda done, total: signals.progress.emit(generation, done, total))
            except Exception as failure:
                error = str(failure)
            finally:
                with self._cache_lock:
                    self._verify_threads.pop(threading.current_thread(), None)
            if not self._closed:
                try:
                    signals.verified.emit(generation, report, error)
                except RuntimeError:
                    pass
        thread = threading.Thread(target=verify, name='cache-deep-verify', daemon=True)
        with self._cache_lock:
            self._verify_threads[thread] = cache
        thread.start()

    def _verify_progress(self, generation, done, total):
        if generation != self._verify_generation or self._closed:
            return
        dialog = getattr(self, '_verify_dialog', None)
        if dialog is not None:
            dialog.setMaximum(total)
            dialog.setValue(done)
            dialog.setLabelText(f'正在深入校验：{done:,} / {total:,} 个文件。照片不会被修改。')

    def _verify_complete(self, generation, report, error):
        if generation != self._verify_generation or self._closed:
            return
        self._verify_running = False
        dialog = getattr(self, '_verify_dialog', None)
        self._verify_dialog = None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        if error:
            self._show_message('深入校验未完成：' + error)
            return
        changed = report.get('changed', ())
        if changed:
            self.load_folders(self.folders, preserve_view=True)
        action = '已取消校验' if report.get('cancelled') else '深入校验完成'
        self._show_message(f"{action}：已检查 {report.get('checked', 0):,} 个文件，更新 {len(changed):,} 项缓存；手动选择已保留。")

    def _show_details(self, item):
        text = f"图片：{item.image_path}\n"
        if item.video_path:
            text += f"视频：{item.video_path}\n"
        if item.candidates:
            text += "\n同名候选：\n" + "\n".join(str(path) for path in item.candidates)
        text += "\n\n" + (item.note or "普通图片，不参与 Live Photo 配对。")
        if item.uncertain:
            text += "\n\n此项存在歧义，可以通过“选择静态图片”手动更正。"
        QMessageBox.information(self, "文件与配对详情", text)

    def _save_preferences(self):
        try:
            self.preferences.save()
        except OSError as error:
            if hasattr(self, "_toast_timer"):
                self._show_message("设置暂时无法保存：" + str(error))

    def _check_disk(self):
        if not self.folders or self._closed:
            return
        missing = frozenset(root for root in self.folders if not root.is_dir())
        lost = missing - self._missing_folders
        restored = self._missing_folders - missing
        self._missing_folders = missing
        self._disk_missing = bool(missing)
        self._update_path_label()
        if lost:
            if self._displayed_item is not None and any(
                    self._displayed_item.image_path.is_relative_to(root) for root in lost):
                self.viewer.stop_live()
            self._verify_cancel.set()
            self._show_message(f"{len(lost)} 个照片文件夹暂时不可访问，已保留索引；其他文件夹仍可浏览。请重新连接硬盘后点击 ↻。")
        elif restored:
            self._show_message(f"{len(restored)} 个照片文件夹已恢复连接，可继续浏览或点击 ↻ 重新扫描。")

    def eventFilter(self, watched, event):
        if self._closed:
            return False
        kind = event.type()
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.ApplicationDeactivate) and watched is self:
            self._keyboard_live_pressed = False
            self._autoplay_armed = False
            self.viewer.stop_live()
        if kind not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return super().eventFilter(watched, event)
        if QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            return False
        active = QApplication.activeWindow()
        if active is not None and active is not self:
            return False
        focus = QApplication.focusWidget()
        editing = isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox,
                                     QKeySequenceEdit, QComboBox))
        if kind == QEvent.Type.KeyRelease and self._keyboard_live_pressed:
            if event.key() == self._keyboard_live_key and not event.isAutoRepeat():
                self._keyboard_live_pressed = False
                self.viewer.release_live()
                return True
        if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape and self._fullscreen_active:
            if not event.isAutoRepeat():
                self._exit_fullscreen()
            return True
        if (kind == QEvent.Type.KeyPress and _matches_shortcut(event, "fullscreen")
                and self._fullscreen_active):
            if not event.isAutoRepeat():
                self._exit_fullscreen()
            return True
        filename_escape = (focus is self.filename_label and kind == QEvent.Type.KeyPress
                           and event.key() == Qt.Key.Key_Escape
                           and event.modifiers() == Qt.KeyboardModifier.NoModifier)
        if editing and not filename_escape:
            return False
        if focus is self.tree and _matches_shortcut(event, 'select_folders'):
            if kind == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self.tree.set_all_checked(True)
            return True
        if kind == QEvent.Type.KeyRelease and not event.isAutoRepeat() and (
                _matches_shortcut(event, "previous") or _matches_shortcut(event, "next")):
            self._flush_navigation()
            return True
        matches_filmstrip = (not self._filmstrip_sequence.isEmpty()
                             and event.keyCombination() == self._filmstrip_sequence[0])
        if matches_filmstrip:
            if kind == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self._toggle_strip_shortcut()
            return True
        matches_once = not self._once_sequence.isEmpty() and event.keyCombination() == self._once_sequence[0]
        if matches_once:
            if kind == QEvent.Type.KeyPress and not event.isAutoRepeat():
                if (not self._grid_mode and self._displayed_item is not None
                        and self.content.currentWidget() is self.viewer):
                    self.viewer.play_live_once()
            return True
        matches_live = not self._live_sequence.isEmpty() and event.keyCombination() == self._live_sequence[0]
        if matches_live:
            if not event.isAutoRepeat():
                if kind == QEvent.Type.KeyPress:
                    if (not self._keyboard_live_pressed and not self._grid_mode
                            and self._displayed_item is not None
                            and self.content.currentWidget() is self.viewer):
                        self._keyboard_live_pressed = True
                        self._keyboard_live_key = event.key()
                        self.viewer.start_live()
                else:
                    self._keyboard_live_pressed = False
                    self.viewer.release_live()
            return True
        if kind == QEvent.Type.KeyPress:
            zoom = _overview_zoom_direction(event.key(), event.modifiers())
            if zoom and self._grid_mode and self.content.currentWidget() is self.grid:
                self.grid.zoom_step(zoom)
                return True
            if _matches_shortcut(event, "previous"):
                if event.isAutoRepeat():
                    self._queue_navigation(-1)
                else:
                    self._navigate(-1)
                return True
            if _matches_shortcut(event, "next"):
                if event.isAutoRepeat():
                    self._queue_navigation(1)
                else:
                    self._navigate(1)
                return True
            if _matches_shortcut(event, "fullscreen") and not event.isAutoRepeat():
                self._toggle_fullscreen()
                return True
            if event.key() == Qt.Key.Key_Escape:
                if not event.isAutoRepeat() and not self._grid_mode:
                    self.grid_button.setChecked(True)
                    self._toggle_grid(True)
                return True
            if _matches_shortcut(event, "open_folder"):
                self.choose_folder()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        if self._closed:
            super().closeEvent(event)
            return
        self._save_preferences()
        self._closed = True
        self._scan_cancel.set()
        self._cancel_verification()
        self.viewer.stop_live()
        self._disk_timer.stop()
        self._analysis_timer.stop()
        self._navigation_timer.stop()
        self._grid_size_save_timer.stop()
        self.jobs.close()
        QApplication.instance().removeEventFilter(self)
        # Running media tasks can still hold the database, especially on
        # Windows where an open SQLite file cannot be removed. Cancel isolated
        # decoding first, join workers off the GUI thread, then close the cache.
        def shutdown():
            try:
                try:
                    close_video_probe()
                finally:
                    self.jobs.wait()
                    # A worker that was already comparing images may have
                    # entered the shared decoder while shutdown was starting.
                    close_video_probe()
                    for thread in tuple(self._load_threads):
                        thread.join()
                    with self._cache_lock:
                        verifiers = tuple(self._verify_threads)
                    for thread in verifiers:
                        thread.join()
                    for thread in tuple(self._cache_reapers):
                        if thread is not threading.current_thread():
                            thread.join()
                    with self._cache_lock:
                        caches = tuple(self._cache_stores.values())
                    for cache in caches:
                        cache.close()
            except Exception as error:
                self._shutdown_error = str(error)
            finally:
                self._shutdown_complete.set()
        self._shutdown_thread = threading.Thread(target=shutdown, name="media-shutdown", daemon=True)
        self._shutdown_thread.start()
        super().closeEvent(event)

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        """Let tests and non-GUI callers wait until media/database handles close."""
        return self._shutdown_complete.wait(timeout) and self._shutdown_error is None


def run(folder: Path | None = None, screenshot: Path | None = None) -> int:
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("LiveViewer")
    application.setOrganizationName("LiveViewer")
    application.setStyle("Fusion")
    icon_path = Path(getattr(sys, "_MEIPASS", app_directory())) / "assets" / "LiveViewer.png"
    if icon_path.is_file():
        application.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    if screenshot is not None:
        complete = [False]
        def capture():
            if complete[0]:
                return
            complete[0] = True
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(screenshot))
            window.close()
            application.quit()
        window.image_ready.connect(lambda: QTimer.singleShot(900, capture))
        QTimer.singleShot(11_500, capture)
    initial = (folder,) if folder is not None else tuple(window.preferences.last_folders)
    if initial:
        QTimer.singleShot(0, lambda: window.load_folders(initial))
    elif screenshot is not None:
        QTimer.singleShot(900, capture)
    result = application.exec()
    window.wait_for_shutdown(3.0)
    return result
