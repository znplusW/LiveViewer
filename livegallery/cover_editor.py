"""Exact-frame Live Photo cover editor with one cancellable decode worker."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QSlider, QVBoxLayout,
)

from .cover_save import CoverSource, SaveResult, save_cover
from .frame_reader import FrameCancelled, FrameReader
from .models import MediaItem


class _Signals(QObject):
    opened = Signal(object)
    frame = Signal(int, int, object)
    saved = Signal(object)
    failed = Signal(str, int, str)
    cancelled = Signal()


def _qimage(image) -> QImage:
    rgb = image.convert("RGB")
    return QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3,
                  QImage.Format.Format_RGB888).copy()


class _EditorWorker:
    """One running request and one latest pending request, never a frame queue."""

    def __init__(self, item, reader_factory, save_function):
        self.signals = _Signals()
        self._item = item
        self._reader_factory = reader_factory
        self._save_function = save_function
        self._condition = threading.Condition()
        self._closing = threading.Event()
        self._save_cancel = threading.Event()
        self._reader = None
        self._pending = None
        self._busy = None
        self._sequence = 0
        self.thread = threading.Thread(target=self._run, name="cover-editor", daemon=True)

    def start(self):
        self.thread.start()

    def request_preview(self, index, sequence):
        with self._condition:
            # Cancel under the same scheduling lock, before exposing the new
            # request. Otherwise a just-finished decode could start the new
            # frame and have that newer operation cancelled by mistake.
            if self._busy == "preview" and self._reader is not None:
                self._reader.cancel_current()
            self._sequence = sequence
            self._pending = ("preview", sequence, index, None)
            self._condition.notify()

    def request_save(self, index, mode):
        self._save_cancel.clear()
        with self._condition:
            if self._busy == "preview" and self._reader is not None:
                self._reader.cancel_current()
            self._pending = ("save", self._sequence, index, mode)
            self._condition.notify()

    def cancel_save(self):
        self._save_cancel.set()
        if self._reader is not None:
            self._reader.cancel_current()

    def close(self):
        self._closing.set()
        self.cancel_save()
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
            self.signals.opened.emit(metadata)
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
                        image = self._reader.preview(index, edge=1200)
                        with self._condition:
                            current = sequence == self._sequence
                        if current and not self._closing.is_set():
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
            if self._reader is not None:
                self._reader.close()


class EditorDialog(QDialog):
    """On acceptance, ``saved_result`` describes the completed disk operation."""

    def __init__(self, item: MediaItem, parent=None, *, reader_factory=None, save_function=None):
        super().__init__(parent)
        self.setWindowTitle("更改头图 — LiveViewer")
        self.resize(1040, 810)
        self.setMinimumSize(740, 560)
        self.saved_result: SaveResult | None = None
        self.metadata = None
        self.selected_frame_index = 0
        self._request_sequence = 0
        self._loaded_sequence = -1
        self._preview_image = QImage()
        self._saving = False
        self._cancel_pending = False
        self._item = item
        self._build_ui()
        self._worker = _EditorWorker(item, reader_factory or FrameReader, save_function or save_cover)
        self._worker.signals.opened.connect(self._opened)
        self._worker.signals.frame.connect(self._frame_ready)
        self._worker.signals.saved.connect(self._saved)
        self._worker.signals.failed.connect(self._failed)
        self._worker.signals.cancelled.connect(self._save_cancelled)
        self.finished.connect(lambda _result: self._worker.close())
        self._worker.start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("选择视频中的一帧，作为这个 Live Photo 的头图")
        title.setStyleSheet("font-size:19px;font-weight:600")
        layout.addWidget(title)
        self.preview = QLabel("正在读取视频…")
        self.preview.setObjectName("coverPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(220)
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
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setObjectName("coverTimeline")
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.valueChanged.connect(self._request_frame)
        layout.addWidget(self.timeline)
        self.previous_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))
        self._previous_shortcut = QShortcut(QKeySequence("Left"), self)
        self._next_shortcut = QShortcut(QKeySequence("Right"), self)
        self._previous_shortcut.activated.connect(lambda: self._step(-1))
        self._next_shortcut.activated.connect(lambda: self._step(1))
        group = QGroupBox("保存方式")
        modes = QVBoxLayout(group)
        self.copy_choice = QRadioButton("创建副本（保留原图片和原视频）")
        self.overwrite_choice = QRadioButton("覆盖原图（不保留备份）")
        self.copy_choice.setChecked(True)
        modes.addWidget(self.copy_choice)
        modes.addWidget(self.overwrite_choice)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        modes.addWidget(self.warning_label)
        self.copy_choice.toggled.connect(self._update_warning)
        self.overwrite_choice.toggled.connect(self._update_warning)
        self._update_warning()
        layout.addWidget(group)
        self.status_label = QLabel("正在读取视频，取消不会更改任何文件。")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存头图")
        self.save_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_controls()

    def _update_warning(self):
        if self.overwrite_choice.isChecked():
            self.warning_label.setText(
                f"将永久替换「{self._item.image_path.name}」，不保留备份，保存后无法在 LiveViewer 中撤销。原视频不会更改。"
            )
            self.warning_label.setStyleSheet("color:#efb789")
        else:
            self.warning_label.setText(
                "图片与视频会一起创建 v1、v2… 副本，并使用同一个版本号；图片格式保持不变，视频逐字节复制，原文件不变。"
            )
            self.warning_label.setStyleSheet("color:#a7b2bf")

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

    def _request_frame(self, index):
        if self.metadata is None or self._saving:
            return
        self.selected_frame_index = max(0, min(len(self.metadata.frames) - 1, int(index)))
        self._request_sequence += 1
        self.save_button.setEnabled(False)
        self.status_label.setText("正在读取所选帧…")
        frame = self.metadata.frames[self.selected_frame_index]
        self.position_label.setText(
            f"第 {self.selected_frame_index + 1:,} / {len(self.metadata.frames):,} 帧  ·  {frame.pts_seconds:.3f} 秒"
        )
        self._update_controls()
        self._worker.request_preview(self.selected_frame_index, self._request_sequence)

    def _step(self, direction):
        if self.metadata is not None and not self._saving:
            self.timeline.setValue(max(0, min(len(self.metadata.frames) - 1,
                                             self.selected_frame_index + direction)))

    def _frame_ready(self, sequence, index, image):
        if sequence != self._request_sequence or index != self.selected_frame_index or self._saving:
            return
        self._loaded_sequence = sequence
        self._preview_image = image
        self._render_preview()
        self.status_label.setText("拖动时间轴选择画面；左右方向键每次移动一个实际视频帧。")
        self.save_button.setEnabled(True)

    def _render_preview(self):
        if not self._preview_image.isNull():
            self.preview.setPixmap(QPixmap.fromImage(self._preview_image).scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def _update_controls(self):
        count = len(self.metadata.frames) if self.metadata is not None else 0
        self.previous_button.setEnabled(not self._saving and self.selected_frame_index > 0)
        self.next_button.setEnabled(not self._saving and self.selected_frame_index + 1 < count)
        self.timeline.setEnabled(not self._saving and count > 0)
        self.copy_choice.setEnabled(not self._saving)
        self.overwrite_choice.setEnabled(not self._saving)

    def _save(self):
        if self._saving or self._loaded_sequence != self._request_sequence:
            return
        self._saving = True
        self._update_controls()
        self.save_button.setEnabled(False)
        self.status_label.setText("正在保存所选的完整分辨率视频帧…")
        self._worker.request_save(self.selected_frame_index, "overwrite" if self.overwrite_choice.isChecked() else "copy")

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
