"""LiveViewer desktop interface and bounded asynchronous media orchestration."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QListView, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QSplitter, QStackedWidget, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QWidgetAction,
)

from .cache import CacheStore
from .catalog_model import CatalogModel, ThumbnailDelegate
from .imaging import load_image
from .jobs import JobQueue
from .library import natural_key, scan_folder
from .matching import close_video_probe, resolve_pair
from .models import MediaItem
from .settings import Preferences, app_directory, data_directory
from .viewer import ViewerCanvas


STYLE = """
QWidget { background: #11151a; color: #e9edf2; font-family: 'Segoe UI', 'PingFang SC', sans-serif; font-size: 13px; }
QLabel { background: transparent; }
QMainWindow, QDialog { background: #11151a; }
QFrame#header { background: #161b22; border-bottom: 1px solid #2a3039; }
QFrame#filterbar { background: #14191f; border-bottom: 1px solid #262d36; }
QFrame#sidebar { background: #151a20; border-right: 1px solid #29303a; }
QFrame#infobar { background: #151a20; border-bottom: 1px solid #242b34; }
QFrame#film { background: #14191f; border-top: 1px solid #2a313b; }
QLabel#brand { color: #f1d19b; font-size: 25px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #83909f; }
QLabel#emptyTitle { color: #e7edf5; font-size: 25px; font-weight: 600; }
QLabel#emptyIcon { color: #e6ba72; font-size: 66px; }
QLabel#emptyBody { color: #8e9baa; font-size: 14px; line-height: 150%; }
QLabel#filename { font-size: 15px; font-weight: 600; }
QLabel#liveBadge { color: #f1cf91; border: 1px solid #775e38; border-radius: 5px; padding: 4px 7px; background: #31281c; }
QLabel#toast { background: #3a3020; color: #f5d69e; border: 1px solid #6b5533; padding: 9px 14px; }
QPushButton { border: 1px solid #39424e; border-radius: 7px; background: #232b35; color: #dce3ec; padding: 8px 13px; min-height: 20px; }
QPushButton:hover { background: #303b48; border-color: #5a697b; }
QPushButton:pressed, QPushButton:checked { background: #463a27; border-color: #bf965a; color: #f4d49f; }
QPushButton#primary { background: #d8ae6b; color: #181b20; border-color: #d8ae6b; font-weight: 600; }
QPushButton#primary:hover { background: #eac187; }
QPushButton:disabled { color: #56616e; border-color: #2b323b; background: #1b2128; }
QLineEdit, QComboBox, QSpinBox, QKeySequenceEdit { padding: 7px 9px; border: 1px solid #35404d; border-radius: 6px; background: #1b222b; min-height: 20px; selection-background-color: #83663e; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QKeySequenceEdit:focus { border-color: #c7a166; }
QComboBox QAbstractItemView { background: #222b36; border: 1px solid #485565; selection-background-color: #57472f; }
QListView, QTreeWidget { border: none; background: transparent; outline: none; }
QTreeWidget::item { min-height: 32px; padding: 2px 4px; border-radius: 5px; }
QTreeWidget::item:selected { background: #3c3326; color: #f1d19b; }
QTreeWidget::item:hover { background: #252d38; }
QScrollBar:horizontal { background: #151a20; height: 10px; border: none; }
QScrollBar:vertical { background: #151a20; width: 10px; border: none; }
QScrollBar::handle { background: #3c4857; border-radius: 4px; min-width: 35px; min-height: 35px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QGroupBox { border: 1px solid #34404c; border-radius: 8px; margin-top: 13px; padding: 20px 13px 12px; }
QGroupBox::title { color: #dfc59b; subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QCheckBox { spacing: 9px; padding: 4px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #586678; border-radius: 3px; background: #202833; }
QCheckBox::indicator:checked { background: #d8ae6b; border: 3px solid #d8ae6b; }
QMenu { background: #202833; border: 1px solid #465260; padding: 6px; }
QMenu::item { padding: 8px 22px; border-radius: 4px; }
QMenu::item:selected { background: #4a3d29; }
QMenu::separator { height: 1px; background: #3a4553; margin: 5px; }
QStatusBar { background: #11161c; color: #7e8c9c; border-top: 1px solid #252c35; font-size: 11px; }
QSplitter::handle { background: #252d37; width: 1px; }
QToolTip { background: #28323e; color: #edf1f7; border: 1px solid #586678; padding: 6px; }
"""


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
        box = QCheckBox(text)
        box.setChecked(checked)
        box.setStyleSheet("QCheckBox { padding: 7px 15px; }")
        action = QWidgetAction(self.menu)
        action.setDefaultWidget(box)
        self.menu.addAction(action)
        box.toggled.connect(self._selection_changed)
        return box

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


def _shortcut_error(hold: QKeySequence, once: QKeySequence) -> str:
    if hold.isEmpty() or once.isEmpty():
        return "请分别设置按住播放、单击播放的按键或组合键。"
    for sequence in (hold, once):
        if sequence[0].key() == Qt.Key.Key_Escape:
            return "Esc 保留用于退出全屏，不能设为播放快捷键。"
    if hold == once:
        return "按住播放与单击播放不能使用相同的快捷键，请修改其中一个。"
    return ""


def _shortcut_overrides(hold: QKeySequence, once: QKeySequence) -> list[str]:
    builtins = {"Left": "上一张", "Right": "下一张", "F11": "进入全屏", "Ctrl+O": "打开文件夹"}
    overrides = []
    for label, sequence in (("按住播放", hold), ("单击播放", once)):
        for key, operation in builtins.items():
            if sequence == QKeySequence(key):
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
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        sections = QVBoxLayout(scroll_content)
        sections.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(scroll_content)
        outer.addWidget(scroll, 1)
        play_group = QGroupBox("Live Photo 播放")
        form = QFormLayout(play_group)
        form.setVerticalSpacing(13)
        self.mode = QComboBox()
        self.mode.addItems(["播放一次，结束后恢复照片", "持续按住时循环播放"])
        self.mode.setCurrentIndex(1 if preferences.loop else 0)
        form.addRow("播放模式", self.mode)
        self.muted = QCheckBox("默认静音")
        self.muted.setChecked(preferences.muted)
        form.addRow("声音", self.muted)
        self.zoom_mode = QComboBox()
        self.zoom_mode.addItems(["沿用照片当前放大区域", "临时完整播放，停止后恢复原视图"])
        self.zoom_mode.setCurrentIndex(0 if preferences.preserve_zoom else 1)
        form.addRow("放大时播放", self.zoom_mode)
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
        help_text = QLabel("按住播放：松开立即停止。单击播放：松开后继续，播完一次恢复照片。\n可设置单个按键或组合键；输入文字时不触发，Esc 保留用于退出全屏。")
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        form.addRow(help_text)
        self.shortcut_note = QLabel()
        self.shortcut_note.setWordWrap(True)
        form.addRow(self.shortcut_note)
        self.override_keys = QCheckBox("确认替换以上原有快捷键")
        form.addRow(self.override_keys)
        self.live_key.keySequenceChanged.connect(self._shortcuts_changed)
        self.live_once_key.keySequenceChanged.connect(self._shortcuts_changed)
        self._shortcuts_changed()
        sections.addWidget(play_group)
        storage_group = QGroupBox("文件夹与本机缓存")
        storage = QFormLayout(storage_group)
        storage.setVerticalSpacing(12)
        self.recursive = QCheckBox("包含所有子文件夹（只在同一目录内配对）")
        self.recursive.setChecked(preferences.recursive)
        storage.addRow(self.recursive)
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
        self.portable = QCheckBox("完全便携：设置和缓存跟随软件文件夹")
        self.portable.setChecked((app_directory() / "portable.flag").exists())
        storage.addRow(self.portable)
        portable_note = QLabel("便携模式切换后重启生效；现有缓存不会自动搬运。照片与视频始终只读。")
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
        screen = self.screen()
        available_height = screen.availableGeometry().height() if screen else 900
        self.resize(620, min(850, max(420, available_height - 70)))

    def _accept(self):
        error = _shortcut_error(self.live_key.keySequence(), self.live_once_key.keySequence())
        if error:
            QMessageBox.information(self, "播放快捷键", error)
            return
        overrides = _shortcut_overrides(self.live_key.keySequence(), self.live_once_key.keySequence())
        if overrides and not self.override_keys.isChecked():
            QMessageBox.information(self, "快捷键冲突", "\n".join(overrides) + "\n\n请勾选确认替换，或更换播放快捷键。")
            return
        self.accept()

    def _shortcuts_changed(self, *_args):
        hold, once = self.live_key.keySequence(), self.live_once_key.keySequence()
        error = _shortcut_error(hold, once)
        overrides = _shortcut_overrides(hold, once)
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
        self.setStyleSheet(STYLE)
        self.preferences = Preferences.load()
        self._data_base = data_directory()
        self.cache = CacheStore(self._data_base / "cache", self.preferences.cache_mb * 1024 ** 2)
        self.jobs = JobQueue(self)
        self.jobs.finished.connect(self._job_finished)
        self._scan_signals = _ScanSignals(self)
        self._scan_signals.progress.connect(self._scan_progress)
        self._scan_signals.complete.connect(self._scan_complete)
        self._scan_cancel = threading.Event()
        self._scan_generation = 0
        self._epoch = 0
        self._closed = False
        self._shutdown_complete = threading.Event()
        self._shutdown_error = None
        self._shutdown_thread = None
        self.folder: Path | None = None
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
        self._background_pair_id = None
        self._remembered_strip = True
        self._live_sequence = QKeySequence(self.preferences.live_key)
        self._once_sequence = QKeySequence(self.preferences.live_once_key)
        self._build_ui()
        self.viewer.set_options(self.preferences.loop, self.preferences.muted,
                                self.preferences.preserve_zoom, self.preferences.hold_ms)
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
        QApplication.instance().installEventFilter(self)
        self._update_hint()
        self.statusBar().showMessage("只读浏览 · JPG / JPEG / HEIC + MOV · 支持触屏与普通图片")

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
        self.strip_button.setChecked(True)
        header.addWidget(self.strip_button)
        self.fullscreen_button = self._button("⛶  全屏", self._toggle_fullscreen)
        self.fullscreen_button.setToolTip("F11 全屏；Esc 返回")
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
        title.setStyleSheet("font-size:12px;color:#83909f;font-weight:600;padding-left:5px")
        side.addWidget(title)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.itemClicked.connect(self._directory_selected)
        side.addWidget(self.tree)
        side_note = QLabel("图片与视频保持原样\n分析和缓存只存放在本机")
        side_note.setObjectName("muted")
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
        self.filename_label = QLabel("你的本地照片库")
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
        self.grid = QListView()
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(ThumbnailDelegate(self.grid, large=True))
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(5)
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
        film_layout.setContentsMargins(12, 8, 12, 5)
        film_layout.setSpacing(2)
        film_header = QHBoxLayout()
        self.library_label = QLabel("尚未加载照片")
        self.library_label.setObjectName("muted")
        film_header.addWidget(self.library_label)
        film_header.addStretch()
        self.analysis_label = QLabel("按需加载 · 本机缓存")
        self.analysis_label.setObjectName("muted")
        film_header.addWidget(self.analysis_label)
        film_layout.addLayout(film_header)
        self.strip = QListView()
        self.strip.setModel(self.model)
        self.strip.setItemDelegate(ThumbnailDelegate(self.strip))
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setUniformItemSizes(True)
        self.strip.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.strip.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setFixedHeight(127)
        self.strip.clicked.connect(lambda index: self._select_row(index.row()))
        self.strip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.strip.customContextMenuRequested.connect(lambda pos: self._list_context_menu(self.strip, pos))
        film_layout.addWidget(self.strip)
        right_layout.addWidget(self.film)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1200])
        root.addWidget(splitter, 1)
        self._update_navigation()

    def choose_folder(self):
        self.viewer.stop_live()
        selected = QFileDialog.getExistingDirectory(self, "选择照片文件夹", str(self.folder or Path.home()))
        if selected:
            self.load_folder(Path(selected))

    def load_folder(self, folder: Path):
        folder = Path(folder).absolute()
        self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        cancel = self._scan_cancel
        self._scan_generation += 1
        generation = self._scan_generation
        self._epoch += 1
        self._request_context.clear()
        self.folder = folder
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
        self.path_label.setText(str(folder))
        self.path_label.setToolTip(str(folder))
        self.filename_label.setText("正在建立照片索引…")
        self.live_badge.hide()
        self.preferences.last_folder = str(folder)
        self._save_preferences()
        self._disk_missing = False
        self._background_pair_id = None
        self._analysis_timer.stop()
        self._update_navigation()
        recursive = self.preferences.recursive

        def scan():
            try:
                items = scan_folder(folder, cancel=cancel.is_set,
                                    progress=lambda count: self._scan_signals.progress.emit(generation, count),
                                    recursive=recursive)
                if not cancel.is_set():
                    self._scan_signals.complete.emit(generation, items, None)
            except Exception as error:
                if not cancel.is_set():
                    self._scan_signals.complete.emit(generation, None, str(error))
        threading.Thread(target=scan, name="folder-index", daemon=True).start()

    def _scan_progress(self, generation, count):
        if generation == self._scan_generation and not self._closed:
            self.empty_body.setText(f"已发现 {count:,} 张图片，正在扫描目录…\n完整图片和视频不会同时读入内存。")
            self.statusBar().showMessage(f"正在扫描 · 已发现 {count:,} 张图片")

    def _scan_complete(self, generation, items, error):
        if generation != self._scan_generation or self._closed:
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
        if not self.model.items:
            self._set_empty("此文件夹中没有支持的图片", "支持 JPG、JPEG、HEIC、PNG、WebP、BMP、GIF、TIFF 和 AVIF。\n可在设置中开启扫描子文件夹。", "◉")
            self.empty_open.show()
            return
        preferred = next((row for row, item in enumerate(self.model.items)
                          if str(item.image_path) == self.preferences.last_image), 0)
        self._select_row(preferred)
        self._analysis_timer.start(1500)
        self._show_message(f"已载入 {len(items):,} 个浏览条目；缩略图和同名候选按需分析。")
        if self.preferences.sort_by == "time":
            self._request_times()

    def _rescan(self):
        if self.folder:
            self.load_folder(self.folder)
        else:
            self.choose_folder()

    def _build_tree(self):
        self.tree.clear()
        if self.folder is None:
            return
        root_item = QTreeWidgetItem([f"全部文件夹  ·  {len(self._items):,}"])
        root_item.setData(0, Qt.ItemDataRole.UserRole, str(self.folder))
        self.tree.addTopLevelItem(root_item)
        entries = {self.folder: root_item}
        for item in self._items:
            relative = item.image_path.parent.relative_to(self.folder)
            path = self.folder
            parent = root_item
            for part in relative.parts:
                path = path / part
                if path not in entries:
                    child = QTreeWidgetItem([part])
                    child.setData(0, Qt.ItemDataRole.UserRole, str(path))
                    parent.addChild(child)
                    entries[path] = child
                parent = entries[path]
        root_item.setExpanded(True)
        self.tree.setCurrentItem(root_item)

    def _directory_selected(self, tree_item):
        self._directory_filter = Path(tree_item.data(0, Qt.ItemDataRole.UserRole))
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
        directory = self._directory_filter
        visible = []
        for item in self._items:
            if directory and not item.image_path.is_relative_to(directory):
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
        self.analysis_label.setText(f"待分析 {sum(item.pending for item in self._primary_items):,} 项 · 本机缓存")

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

    def _select_row(self, row):
        if not 0 <= row < len(self.model.items):
            return
        item = self.model.items[row]
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
        if item.pending:
            self._awaiting_pair = item.id
            self._set_empty("正在选择静态照片", "比较同名候选图片与视频画面，结果会保存在本机。\n你可以继续切换其他照片。", "◌")
            self._request_pair(item, 0)
        else:
            self._awaiting_pair = None
            self._load_current(item)
        self._update_navigation()
        self._update_hint()
        self._prefetch_neighbors(row)

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
        self._set_empty("正在载入照片", item.image_path.name + "\n大图按需加载，其他照片仍可继续浏览。", "◌")
        self._submit("image", str(item.image_path), lambda: _decode(item.image_path, 4800), 0,
                     (self._epoch, item.id, item.image_path))

    def _display_image(self, item, image, original_size):
        if self._closed or item.id != self._current_id:
            return
        self._original_size = original_size
        self._current_full = max(original_size) <= 4800
        self._displayed_item = item
        self.viewer.set_photo(image, item.video_path)
        self.content.setCurrentWidget(self.grid if self._grid_mode else self.viewer)
        self.filename_label.setText(item.image_path.name)
        self.live_badge.setVisible(item.is_live)
        self.live_badge.setText("! LIVE" if item.uncertain else "◉ LIVE")
        self.preferences.last_image = str(item.image_path)
        self.statusBar().showMessage(item.note or f"{original_size[0]:,} × {original_size[1]:,} · {item.image_path.parent}")
        self._update_hint()
        self.image_ready.emit()

    def _cache_preview(self, path, image, size):
        key = str(path)
        previous = self._previews.pop(key, None)
        if previous:
            self._preview_bytes -= previous[0].sizeInBytes()
        self._previews[key] = (image, size)
        self._preview_bytes += image.sizeInBytes()
        while len(self._previews) > 3 or self._preview_bytes > 200 * 1024 ** 2:
            if len(self._previews) <= 1:
                break
            _, (old_image, _) = self._previews.popitem(last=False)
            self._preview_bytes -= old_image.sizeInBytes()

    def _request_thumbnail(self, path):
        epoch = self._epoch
        def thumbnail():
            target = self.cache.get_thumbnail(path)
            return QImage(str(target))
        self._submit("thumb", str(path), thumbnail, 7, (epoch, path))

    def _request_pair(self, item, priority):
        revision = self._pair_revisions.get(item.id, 0)
        self._submit("pair", f"{item.id}:{revision}", lambda: resolve_pair(item, self.cache), priority,
                     (self._epoch, item, revision))

    def _schedule_background_pair(self):
        """Gradually expose distinct candidate images without flooding the disk.

        Only one low-priority analysis is outstanding at a time. Current and
        adjacent items may independently request higher-priority analysis.
        Loading/playing a live clip pauses new background submissions.
        """
        if self._closed or self._background_pair_id is not None:
            return
        if self._is_playing or self.viewer._play_requested or self._disk_missing:
            self._analysis_timer.start(1200)
            return
        pending_ids = {context[2].id for context in self._request_context.values()
                       if context[0] == "pair" and context[1] == self._epoch}
        item = next((item for item in self._primary_items if item.pending and item.id not in pending_ids), None)
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
            if item.pending:
                self._request_pair(item, 4)
            elif str(item.image_path) not in self._previews:
                self._submit("image", str(item.image_path), lambda path=item.image_path: _decode(path, 4800), 5,
                             (self._epoch, item.id, item.image_path))

    def _submit(self, kind, identity, function, priority, context):
        key = f"{kind}:{self._epoch}:{identity}"
        if key not in self._request_context:
            self._request_context[key] = (kind, *context)
            epoch = self._epoch
            def guarded():
                if self._closed or epoch != self._epoch:
                    return None
                return function()
            if not self.jobs.submit(key, guarded, priority):
                self._request_context.pop(key, None)

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
            if current_before == original.id and self._awaiting_pair == original.id:
                self._awaiting_pair = None
                self._load_current(selected)
            elif current_before == original.id and self._displayed_item:
                self.statusBar().showMessage("自动择图结果已保存，下次进入该项时应用。")
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

    def _set_empty(self, title, body, icon="◉"):
        self.empty_title.setText(title)
        self.empty_body.setText(body)
        self.empty_icon.setText(icon)
        if not self._grid_mode:
            self.content.setCurrentWidget(self.empty_page)

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
        self.film.setVisible(checked and not self._grid_mode)

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
        self.viewer_tools.setVisible(not checked and self._chrome_visible)

    def _grid_selected(self, index):
        self._grid_mode = False
        self.grid_button.setChecked(False)
        self._toggle_grid(False)
        self._select_row(index.row())

    def _toggle_fullscreen(self):
        """Enter immediately; Esc is the only in-app way to leave fullscreen."""
        if self._fullscreen_active:
            return
        self.viewer.stop_live()
        self._keyboard_live_pressed = False
        self._pre_fullscreen_geometry = self.saveGeometry()
        self._pre_fullscreen_maximized = self.isMaximized()
        if self._grid_mode:
            self.grid_button.setChecked(False)
            self._toggle_grid(False)
        self._fullscreen_active = True
        self._toast_timer.stop()
        self._set_chrome(False)
        self.showFullScreen()
        self.viewer.setFocus(Qt.FocusReason.OtherFocusReason)

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
        self.library_label.setVisible(visible)
        self.analysis_label.setVisible(visible)
        self.sidebar.setVisible(visible and self.sidebar_button.isChecked())
        self.statusBar().setVisible(visible)
        if not visible:
            self.toast.hide()

    def _toggle_controls(self):
        if not self._fullscreen_active:
            self.strip_button.setChecked(not self.strip_button.isChecked())
            self._toggle_strip(self.strip_button.isChecked())

    def _toggle_sound(self, muted):
        self.preferences.muted = muted
        self.viewer.set_options(self.preferences.loop, muted,
                                self.preferences.preserve_zoom, self.preferences.hold_ms)
        self._save_preferences()
        self.sound_button.setText("静音" if muted else "声音开启")

    def _update_hint(self):
        key = QKeySequence(self.preferences.live_key).toString(QKeySequence.SequenceFormat.NativeText)
        once = QKeySequence(self.preferences.live_once_key).toString(QKeySequence.SequenceFormat.NativeText)
        self.hint_label.setText(f"按住鼠标／触屏／{key} 播放 · 松开停止 · 按 {once} 播完一次 · 左右滑动切图 · 双指缩放")

    def open_settings(self):
        self.viewer.stop_live()
        dialog = SettingsDialog(self.preferences, self)
        was_portable = (app_directory() / "portable.flag").exists()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.preferences.loop = dialog.mode.currentIndex() == 1
        self.preferences.muted = dialog.muted.isChecked()
        self.preferences.preserve_zoom = dialog.zoom_mode.currentIndex() == 0
        self.preferences.hold_ms = dialog.hold.value()
        self.preferences.live_key = dialog.live_key.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        self.preferences.live_once_key = dialog.live_once_key.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        recursive_changed = self.preferences.recursive != dialog.recursive.isChecked()
        self.preferences.recursive = dialog.recursive.isChecked()
        self.preferences.cache_mb = dialog.cache.value()
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
        self.viewer.set_options(self.preferences.loop, self.preferences.muted,
                                self.preferences.preserve_zoom, self.preferences.hold_ms)
        self.sound_button.setChecked(self.preferences.muted)
        self.sound_button.setText("静音" if self.preferences.muted else "声音开启")
        self._update_hint()
        self._save_preferences()
        if portable_changed:
            self._show_message("设置已保存到新模式目录。请重启 LiveViewer；现有缓存保留原位。")
        elif recursive_changed and self.folder:
            self.load_folder(self.folder)
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
        self.viewer.stop_live()
        menu = QMenu(self)
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
        menu.addAction("打开所在文件夹", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.image_path.parent))))
        menu.addAction("打开缓存位置", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.cache.base))))
        menu.addSeparator()
        menu.addAction("重新扫描当前文件夹", self._rescan)
        menu.exec(position)

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
        self._submit("clear", "thumbnails", self.cache.clear_thumbnails, 2, (self._epoch,))

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
        if self.folder is None or self._closed:
            return
        available = self.folder.is_dir()
        if not available and not self._disk_missing:
            self._disk_missing = True
            self.viewer.stop_live()
            self._show_message("照片文件夹暂时不可访问。已保留索引，请重新连接硬盘后点击 ↻。")
        elif available and self._disk_missing:
            self._disk_missing = False
            self._show_message("照片文件夹已恢复连接，可继续浏览或点击 ↻ 重新扫描。")

    def eventFilter(self, watched, event):
        if self._closed:
            return False
        kind = event.type()
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.ApplicationDeactivate) and watched is self:
            self._keyboard_live_pressed = False
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
        if editing:
            return False
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
            if event.key() == Qt.Key.Key_Left and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                self._navigate(-1)
                return True
            if event.key() == Qt.Key.Key_Right and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                self._navigate(1)
                return True
            if (event.key() == Qt.Key.Key_F11 and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    and not event.isAutoRepeat()):
                self._toggle_fullscreen()
                return True
            if event.key() == Qt.Key.Key_Escape:
                if self._grid_mode:
                    self.grid_button.setChecked(False)
                    self._toggle_grid(False)
                else:
                    self.viewer.fit_image()
                return True
            if event.key() == Qt.Key.Key_O and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
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
        self.viewer.stop_live()
        self._disk_timer.stop()
        self._analysis_timer.stop()
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
                    self.cache.close()
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
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#11151a"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e9edf2"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#1b222b"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e9edf2"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#232b35"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e9edf2"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#83663e"))
    application.setPalette(palette)
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
    initial = folder
    if initial is None and window.preferences.last_folder:
        remembered = Path(window.preferences.last_folder)
        if remembered.is_dir():
            initial = remembered
    if initial is not None:
        QTimer.singleShot(0, lambda: window.load_folder(initial))
    elif screenshot is not None:
        QTimer.singleShot(900, capture)
    result = application.exec()
    window.wait_for_shutdown(3.0)
    return result
