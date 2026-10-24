"""Responsive exact-frame Live Photo cover editor and delayed save choices."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QEventPoint, QImage, QKeySequence, QPainter, QPen, QPixmap, QPolygonF, QShortcut
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QSlider, QSizePolicy, QStyle, QStyleOptionSlider, QVBoxLayout,
)

from .cover_save import CoverSource, SaveResult, save_cover
from .frame_reader import FrameCancelled, FrameReader
from .models import MediaItem


class _Signals(QObject):
    opened = Signal(object)
    frame = Signal(int, int, object)
    position = Signal(object)
    saved = Signal(object)
    failed = Signal(str, int, str)
    cancelled = Signal()


def _qimage(image) -> QImage:
    rgb = image.convert("RGB")
    return QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3,
                  QImage.Format.Format_RGB888).copy()


class _EditorWorker:
    """Keep one active exact read and one newest request, without starvation.

    Normal videos use the independent bounded PreviewSession. A custom reader
    can still provide on-demand previews; an already started preview completes
    while subsequent navigation replaces only the pending request.
    """

    def __init__(self, item, reader_factory, save_function, preview_factory=None):
        self.signals = _Signals()
        self._item = item
        self._reader_factory = reader_factory
        self._save_function = save_function
        self._preview_factory = preview_factory
        self._condition = threading.Condition()
        self._closing = threading.Event()
        self._save_cancel = threading.Event()
        self._reader = None
        self._preview = None
        self._pending = None
        self._busy = None
        self._sequence = 0
        self._selected_index = 0
        self._request_sequences = {}
        self.thread = threading.Thread(target=self._run, name="cover-editor", daemon=True)

    def start(self):
        self.thread.start()

    def request_preview(self, index, sequence):
        cached = None
        with self._condition:
            if self._closing.is_set():
                return
            self._sequence = sequence
            self._selected_index = index
            self._request_sequences.pop(index, None)
            self._request_sequences[index] = sequence
            while len(self._request_sequences) > 512:
                self._request_sequences.pop(next(iter(self._request_sequences)))
            preview = self._preview
            if preview is not None:
                cached = preview.get(index)
            if cached is None:
                self._pending = ("preview", sequence, index, None)
                self._condition.notify()
            else:
                self._pending = None
        if cached is not None:
            self.signals.frame.emit(sequence, index, _qimage(cached))
            # The backend also needs cached selections for latest-demand
            # tracking and quiet-time refinement. Invoke it outside our lock.
            preview.request(index)

    def _preview_failed(self, text):
        with self._condition:
            sequence = self._sequence
        self.signals.failed.emit("preview", sequence, str(text))

    def _cached_frame(self, index, image):
        with self._condition:
            if self._closing.is_set() or self._busy == "save":
                return
            sequence = self._request_sequences.get(index)
            if sequence is None:
                return
        self.signals.frame.emit(sequence, index, _qimage(image))

    def request_save(self, index, mode):
        self._save_cancel.clear()
        with self._condition:
            # A full-resolution export must wait for the primary reader's
            # current preview to finish. Navigation itself never cancels it.
            self._pending = ("save", self._sequence, index, mode)
            self._condition.notify()

    def cancel_save(self):
        self._save_cancel.set()
        if self._reader is not None:
            self._reader.cancel_current()

    def close(self):
        self._closing.set()
        self.cancel_save()
        preview = self._preview
        if preview is not None:
            preview.close()
        with self._condition:
            self._pending = None
            self._condition.notify()

    def _run(self):
        try:
            self._reader = self._reader_factory(self._item.video_path, cancel_event=self._closing)
            source = CoverSource.capture(self._item, self._closing)
            metadata = self._reader.open()
            if self._closing.is_set():
                return
            if self._preview_factory is not None:
                options = dict(image_path=self._item.image_path,
                               on_frame=self._cached_frame,
                               on_position=self.signals.position.emit,
                               on_error=self._preview_failed,
                               cancel_event=self._closing)
                if (getattr(self._preview_factory, "supports_reader_fork", False)
                        and callable(getattr(self._reader, "fork", None))):
                    # Reuse verified frame metadata, while the session forks an
                    # independent decoder/process and cancellation lifetime.
                    options["reader"] = self._reader
                self._preview = self._preview_factory(self._item.video_path, metadata, **options)
            self.signals.opened.emit(metadata)
            if self._preview is not None:
                self._preview.start()
            while not self._closing.is_set():
                with self._condition:
                    while self._pending is None and not self._closing.is_set():
                        self._condition.wait()
                    if self._closing.is_set():
                        break
                    kind, sequence, index, mode = self._pending
                    self._pending = None
                    self._busy = kind
                try:
                    if kind == "preview":
                        if self._preview is not None:
                            cached = self._preview.get(index)
                            if cached is not None:
                                self._cached_frame(index, cached)
                            else:
                                self._preview.request(index)
                        else:
                            image = self._reader.preview(index, edge=1200)
                            if not self._closing.is_set():
                                self.signals.frame.emit(sequence, index, _qimage(image))
                    else:
                        if self._save_cancel.is_set():
                            raise InterruptedError("已取消保存。")
                        image = self._reader.export(index)
                        result = self._save_function(source, image, mode, cancel_event=self._save_cancel)
                        self.signals.saved.emit(result)
                except (FrameCancelled, InterruptedError):
                    if kind == "save" and not self._closing.is_set():
                        self.signals.cancelled.emit()
                except Exception as error:
                    if not self._closing.is_set():
                        self.signals.failed.emit(kind, sequence, str(error))
                finally:
                    with self._condition:
                        self._busy = None
        except (FrameCancelled, InterruptedError):
            pass
        except Exception as error:
            if not self._closing.is_set():
                self.signals.failed.emit("open", 0, str(error))
        finally:
            if self._preview is not None:
                self._preview.close()
                wait = getattr(self._preview, "wait", None)
                if wait is not None:
                    wait(2)
            if self._reader is not None:
                self._reader.close()


class CoverTimelineSlider(QSlider):
    """A normal tracking slider with a separate, fixed original-cover marker."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.original_index = None
        self._touch_active = False
        self._touch_point_id = None
        self._suppress_touch_mouse_until = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setTracking(True)
        self.setMinimumHeight(30)

    def set_original_index(self, index):
        self.original_index = None if index is None else max(self.minimum(), min(self.maximum(), int(index)))
        self.update()

    def _geometry(self):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderHandle, self)
        return option, groove, handle

    def _value_at(self, position):
        option, groove, handle = self._geometry()
        horizontal = self.orientation() == Qt.Orientation.Horizontal
        groove_start = groove.x() if horizontal else groove.y()
        groove_length = groove.width() if horizontal else groove.height()
        handle_length = handle.width() if horizontal else handle.height()
        coordinate = position.x() if horizontal else position.y()
        span = max(1, groove_length - handle_length)
        offset = round(coordinate - groove_start - handle_length / 2)
        # QTouchEvent positions are widget-local logical pixels, as are these
        # style rectangles. Multiplying by the display scale would mis-seek.
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), max(0, min(span, offset)),
                                              span, option.upsideDown)

    def _finish_touch(self):
        was_active = self._touch_active
        self._touch_active = False
        self._touch_point_id = None
        self._suppress_touch_mouse_until = time.monotonic() + .8
        if was_active:
            self.setSliderDown(False)

    def event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove):
            synthesized = event.source() != Qt.MouseEventSource.MouseEventNotSynthesized
            if synthesized and (self._touch_active or time.monotonic() < self._suppress_touch_mouse_until):
                event.accept()
                return True
            if self._touch_active:
                if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                    # A real mouse can take over immediately. Its following
                    # move/release belongs to QSlider's ordinary mouse path.
                    self._finish_touch()
                else:
                    # Hover or an unrelated mouse release must not end the
                    # finger-owned slider drag or replace its position.
                    event.accept()
                    return True
        if kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                        QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            if kind in (QEvent.Type.EnabledChange, QEvent.Type.WindowDeactivate) and self._touch_active:
                self._finish_touch()
            return super().event(event)

        event.accept()
        if kind == QEvent.Type.TouchCancel:
            self._finish_touch()
            return True
        points = event.points()
        if kind == QEvent.Type.TouchBegin:
            if not self.isEnabled() or len(points) != 1:
                self._finish_touch()
                return True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._touch_active = True
            self._touch_point_id = points[0].id()
            self._suppress_touch_mouse_until = time.monotonic() + .8
            self.setSliderDown(True)
            self.setSliderPosition(self._value_at(points[0].position()))
            return True
        if not self._touch_active:
            # A canceled sequence may still deliver an Update/End. Consume it
            # without moving the handle or starting a new drag.
            return True
        if len(points) != 1 or points[0].id() != self._touch_point_id:
            self._finish_touch()
            return True
        self.setSliderPosition(self._value_at(points[0].position()))
        if kind == QEvent.Type.TouchEnd or points[0].state() == QEventPoint.State.Released:
            self._finish_touch()
        return True

    def hideEvent(self, event):
        if self._touch_active:
            self._finish_touch()
        super().hideEvent(event)

    def focusOutEvent(self, event):
        if self._touch_active:
            self._finish_touch()
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            _, _, handle = self._geometry()
            if not handle.contains(event.position().toPoint()):
                self.setValue(self._value_at(event.position()))
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.original_index is None or self.maximum() <= self.minimum():
            return
        option, groove, handle = self._geometry()
        horizontal = self.orientation() == Qt.Orientation.Horizontal
        span = max(1, (groove.width() - handle.width()) if horizontal else (groove.height() - handle.height()))
        offset = QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), self.original_index,
                                                span, option.upsideDown)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#8cc7db")
        painter.setPen(QPen(color, 2))
        if horizontal:
            x = groove.x() + handle.width() / 2 + offset
            center = groove.center().y()
            painter.drawLine(QPointF(x, max(2, center - 11)), QPointF(x, min(self.height() - 2, center + 9)))
            triangle = [QPointF(x - 4, max(1, center - 13)), QPointF(x + 4, max(1, center - 13)),
                        QPointF(x, max(6, center - 7))]
        else:
            y = groove.y() + handle.height() / 2 + offset
            center = groove.center().x()
            painter.drawLine(QPointF(max(2, center - 11), y), QPointF(min(self.width() - 2, center + 9), y))
            triangle = [QPointF(max(1, center - 13), y - 4), QPointF(max(1, center - 13), y + 4),
                        QPointF(max(6, center - 7), y)]
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF(triangle))


class SaveModeDialog(QDialog):
    """Choose copy/overwrite only after the editor's Save button is pressed."""

    def __init__(self, item: MediaItem, parent=None):
        super().__init__(parent)
        self._item = item
        self.setWindowTitle("保存封面图")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        title = QLabel("选择封面图的保存方式")
        title.setStyleSheet("font-size:18px;font-weight:600")
        layout.addWidget(title)
        group = QGroupBox("保存方式")
        choices = QVBoxLayout(group)
        self.copy_choice = QRadioButton("创建副本（保留原图片和原视频）")
        self.overwrite_choice = QRadioButton("覆盖原图（不保留备份）")
        self.copy_choice.setChecked(True)
        choices.addWidget(self.copy_choice)
        choices.addWidget(self.overwrite_choice)
        layout.addWidget(group)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setMinimumWidth(440)
        layout.addWidget(self.warning_label)
        self.copy_choice.toggled.connect(self._update_warning)
        self.overwrite_choice.toggled.connect(self._update_warning)
        self._update_warning()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("确认保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回编辑")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def mode(self):
        return "overwrite" if self.overwrite_choice.isChecked() else "copy"

    def _update_warning(self):
        if self.overwrite_choice.isChecked():
            self.warning_label.setText(
                f"新图片校验通过后，永久替换「{self._item.image_path.name}」。不保留备份，保存后无法在 LiveViewer 中撤销；原视频不变。"
            )
            self.warning_label.setStyleSheet("color:#efb789")
        else:
            self.warning_label.setText(
                "图片与视频一起创建 v1、v2… 副本，使用同一个版本号。图片格式保持不变，视频完整复制，原文件不变。"
            )
            self.warning_label.setStyleSheet("color:#a7b2bf")


_DEFAULT_PREVIEW = object()


class EditorDialog(QDialog):
    """On acceptance, ``saved_result`` describes the completed disk operation."""

    def __init__(self, item: MediaItem, parent=None, *, reader_factory=None, save_function=None,
                 preview_factory=_DEFAULT_PREVIEW, save_dialog_factory=None):
        super().__init__(parent)
        self.setWindowTitle("更改封面图 — LiveViewer")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        width = min(1040, max(520, available.width() - 60)) if available is not None else 1040
        height = min(810, max(380, available.height() - 70)) if available is not None else 810
        self.setMinimumSize(min(740, width), min(520, height))
        self.resize(width, height)
        self.saved_result: SaveResult | None = None
        self.metadata = None
        self.selected_frame_index = 0
        self.displayed_frame_index = None
        self.original_position = None
        self._request_sequence = 0
        self._displayed_sequence = -1
        self._loaded_sequence = -1
        self._preview_image = QImage()
        self._saving = False
        self._choosing_save = False
        self._cancel_pending = False
        self._item = item
        self._save_dialog_factory = save_dialog_factory or SaveModeDialog
        if preview_factory is _DEFAULT_PREVIEW:
            if reader_factory is None or reader_factory is FrameReader:
                from .cover_preview import PreviewSession
                preview_factory = PreviewSession
            else:
                preview_factory = None
        self._build_ui()
        self._worker = _EditorWorker(item, reader_factory or FrameReader, save_function or save_cover,
                                     preview_factory or None)
        self._worker.signals.opened.connect(self._opened)
        self._worker.signals.frame.connect(self._frame_ready)
        self._worker.signals.position.connect(self._position_ready)
        self._worker.signals.saved.connect(self._saved)
        self._worker.signals.failed.connect(self._failed)
        self._worker.signals.cancelled.connect(self._save_cancelled)
        self.finished.connect(lambda _result: self._worker.close())
        self._worker.start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("选择视频中的一帧，作为这个 Live Photo 的封面图")
        title.setStyleSheet("font-size:19px;font-weight:600")
        title.setWordWrap(True)
        layout.addWidget(title)
        self.preview = QLabel("正在读取视频…")
        self.preview.setObjectName("coverPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(max(140, min(280, self.height() - 300)))
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview.setStyleSheet("background:#080a0d;border:1px solid #303946;border-radius:7px")
        layout.addWidget(self.preview, 1)
        self.resolution_label = QLabel("视频帧分辨率可能低于原照片。")
        self.resolution_label.setWordWrap(True)
        layout.addWidget(self.resolution_label)
        controls = QHBoxLayout()
        self.previous_button = QPushButton("◀ 上一帧")
        self.next_button = QPushButton("下一帧 ▶")
        self.position_label = QLabel("正在建立逐帧时间轴…")
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.position_label, 1)
        controls.addWidget(self.next_button)
        layout.addLayout(controls)
        self.timeline = CoverTimelineSlider(self)
        self.timeline.setObjectName("coverTimeline")
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.valueChanged.connect(self._request_frame)
        layout.addWidget(self.timeline)
        self.original_label = QLabel("正在定位原封面图…")
        self.original_label.setStyleSheet("color:#8cc7db")
        self.original_label.setWordWrap(True)
        layout.addWidget(self.original_label)
        self.previous_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))
        self._previous_shortcut = QShortcut(QKeySequence("Left"), self)
        self._next_shortcut = QShortcut(QKeySequence("Right"), self)
        self._previous_shortcut.setAutoRepeat(True)
        self._next_shortcut.setAutoRepeat(True)
        self._previous_shortcut.activated.connect(lambda: self._step(-1))
        self._next_shortcut.activated.connect(lambda: self._step(1))
        self.status_label = QLabel("正在读取视频…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存封面图")
        self.save_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_controls()

    def _opened(self, metadata):
        self.metadata = metadata
        count = len(metadata.frames)
        if not count:
            self._failed("open", 0, "视频中没有可读取的画面。")
            return
        self.timeline.setRange(0, count - 1)
        self.timeline.setEnabled(True)
        self.resolution_label.setText(
            f"保存分辨率：{metadata.width:,} × {metadata.height:,} 像素。视频帧分辨率可能低于原照片。"
        )
        self._request_frame(0)
        if self.original_position is not None:
            self._position_ready(self.original_position)
        elif self._worker._preview_factory is None:
            self.original_label.setText("原封面图位置尚未匹配")

    def _request_frame(self, index):
        if self.metadata is None or self._saving or self._choosing_save:
            return
        self.selected_frame_index = max(0, min(len(self.metadata.frames) - 1, int(index)))
        self._request_sequence += 1
        self.save_button.setEnabled(False)
        frame = self.metadata.frames[self.selected_frame_index]
        self.position_label.setText(
            f"第 {self.selected_frame_index + 1:,} / {len(self.metadata.frames):,} 帧  ·  {frame.pts_seconds:.3f} 秒"
        )
        self._update_loading_status()
        self._update_controls()
        self._worker.request_preview(self.selected_frame_index, self._request_sequence)

    def _step(self, direction):
        if self.metadata is not None and not self._saving and not self._choosing_save:
            self.timeline.setValue(max(0, min(len(self.metadata.frames) - 1,
                                             self.selected_frame_index + direction)))

    def _frame_ready(self, sequence, index, image):
        if (self._saving or sequence < self._displayed_sequence or sequence > self._request_sequence
                or self.metadata is None or not 0 <= index < len(self.metadata.frames)):
            return
        # An in-flight preview is useful while navigation continues, but it is
        # never treated as the exact selected frame or allowed to enable Save.
        self._displayed_sequence = sequence
        self.displayed_frame_index = index
        self._preview_image = image
        self._render_preview()
        exact = sequence == self._request_sequence and index == self.selected_frame_index
        if exact:
            self._loaded_sequence = sequence
            self.status_label.setText("拖动时间轴选择画面；左右方向键逐帧查看。")
        else:
            self._update_loading_status()
        self.save_button.setEnabled(exact and not self._choosing_save)

    def _update_loading_status(self):
        target = self.selected_frame_index + 1
        if self.displayed_frame_index is None:
            self.status_label.setText(f"正在读取第 {target:,} 帧…")
        else:
            self.status_label.setText(f"正在读取第 {target:,} 帧；当前预览第 {self.displayed_frame_index + 1:,} 帧…")

    def _position_ready(self, position):
        self.original_position = position
        if self.metadata is None:
            return
        if position is None or getattr(position, "index", None) is None:
            self.timeline.set_original_index(None)
            self.original_label.setText(getattr(position, "reason", "") or "未找到可匹配的原封面图位置")
            return
        index = max(0, min(len(self.metadata.frames) - 1, int(position.index)))
        self.timeline.set_original_index(index)
        seconds = self.metadata.frames[index].pts_seconds
        approximate = bool(getattr(position, "approximate", True))
        suffix = "（近似匹配）" if approximate else ""
        self.original_label.setText(f"◆ 原封面图：第 {index + 1:,} 帧 · {seconds:.3f} 秒{suffix}")
        explanation = "根据当前静态图片与视频画面匹配。静态照片与视频可能存在裁切或拍摄时刻差异，标记为匹配位置。"
        self.original_label.setToolTip(explanation)
        self.timeline.setToolTip(self.original_label.text() + "\n" + explanation)

    def _render_preview(self):
        if not self._preview_image.isNull():
            self.preview.setPixmap(QPixmap.fromImage(self._preview_image).scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def _update_controls(self):
        count = len(self.metadata.frames) if self.metadata is not None else 0
        enabled = not self._saving and not self._choosing_save
        self.previous_button.setEnabled(enabled and self.selected_frame_index > 0)
        self.next_button.setEnabled(enabled and self.selected_frame_index + 1 < count)
        self.timeline.setEnabled(enabled and count > 0)

    def _save(self):
        if self._saving or self._choosing_save or self._loaded_sequence != self._request_sequence:
            return
        self._choosing_save = True
        self._update_controls()
        self.save_button.setEnabled(False)
        dialog = self._save_dialog_factory(self._item, self)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            mode = dialog.mode if accepted else None
        finally:
            dialog.deleteLater()
            self._choosing_save = False
        if not accepted:
            self._update_controls()
            self.save_button.setEnabled(self._loaded_sequence == self._request_sequence)
            return
        self._saving = True
        self._update_controls()
        self.save_button.setEnabled(False)
        self.status_label.setText("正在保存所选的完整分辨率视频帧…")
        self._worker.request_save(self.selected_frame_index, mode)

    def _saved(self, result):
        self._saving = False
        self.saved_result = result
        self.accept()

    def _failed(self, kind, sequence, text):
        if kind == "preview" and sequence != self._request_sequence:
            return
        if self._cancel_pending:
            self._saving = False
            super().reject()
            return
        self._saving = False
        self.status_label.setText("无法完成操作：" + text)
        self._update_controls()
        self.save_button.setEnabled(kind == "save" and self._loaded_sequence == self._request_sequence)

    def _save_cancelled(self):
        self._saving = False
        if self._cancel_pending:
            super().reject()
        else:
            self.status_label.setText("已取消保存，原文件未更改。")
            self._update_controls()
            self.save_button.setEnabled(self._loaded_sequence == self._request_sequence)

    def reject(self):
        if self._saving:
            self._cancel_pending = True
            self.status_label.setText("正在取消保存，请稍候…")
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False)
            self._worker.cancel_save()
            return
        super().reject()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_preview_image"):
            self._render_preview()
