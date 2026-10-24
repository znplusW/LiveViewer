"""Photo and held-to-play Live Photo canvas.

The photo and video share one scene coordinate system. A video is fitted inside
the original photo rectangle, so it follows the same pan and zoom without being
stretched when its aspect ratio differs. Touch input is handled on the viewport
instead of being translated into mouse events.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QContextMenuEvent,
    QEventPoint,
    QImage,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)


class ViewerCanvas(QGraphicsView):
    """Scrollable photo canvas; ``zoom_factor() == 1`` means fit-to-window.

    ``start_live`` / ``stop_live`` are also the keyboard press/release entry
    points. The main window owns the keyboard binding and should ignore key
    auto-repeat. ``playback_changed`` becomes true only after a valid video frame
    has arrived, while a slow or failed video leaves the original photo visible.
    """

    navigate_requested = Signal(int)
    zoom_changed = Signal(float)
    playback_changed = Signal(bool)
    message = Signal(str)
    controls_toggled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

        self._photo = QGraphicsPixmapItem()
        self._photo.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._photo.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.scene().addItem(self._photo)
        self._video = QGraphicsVideoItem()
        self._video.setZValue(1)
        self._video.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._video.setVisible(False)
        self.scene().addItem(self._video)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(True)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video)
        self._player.mediaStatusChanged.connect(self._media_status_changed)
        self._player.errorOccurred.connect(self._media_error)
        self._frame_slot = None
        self._generation = 0
        self._video_path: Path | None = None
        self._image_rect = QRectF()
        self._fit_scale = 1.0
        self._fit_mode = True
        self._loop = False
        self._preserve_zoom = True
        self._hold_ms = 300
        self._play_requested = False
        self._live_visible = False
        self._saved_view: tuple[QTransform, QPointF, bool] | None = None
        self._temporary_fit_applied = False

        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._hold_timeout)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(12_000)
        self._load_timer.timeout.connect(self._load_timed_out)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.controls_toggled)

        self._mouse_down = False
        self._mouse_moved = False
        self._pointer_start = QPointF()
        self._pointer_last = QPointF()
        self._hold_consumed = False
        self._releasing_capture = False
        self._touch_active = False
        self._touch_had_multi = False
        self._touch_was_fit = True
        self._touch_count = 0
        self._pinch_distance = 0.0
        self._pinch_center = QPointF()
        self._ignore_mouse_until = 0.0
        self._last_tap_time = 0.0
        self._last_tap_position = QPointF()
        self._window_filter_target: QWidget | None = None

    def set_photo(self, image: QImage, video_path: Path | None = None) -> None:
        """Replace the current image, cancelling every pending playback request."""
        self.stop_live()
        self._click_timer.stop()
        self._player.setSource(QUrl())
        self._video.videoSink().setVideoFrame(QVideoFrame())
        self._video_path = Path(video_path) if video_path else None
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(1.0)
        self._photo.setPixmap(pixmap)
        self._photo.setVisible(True)
        self._image_rect = QRectF(0, 0, image.width(), image.height())
        self.setSceneRect(self._image_rect)
        self._video.setSize(QSizeF(image.width(), image.height()))
        self._video.setOffset(QPointF())
        self._fit_view()

    def clear(self) -> None:
        self.set_photo(QImage())

    def replace_photo(self, image: QImage) -> None:
        """Upgrade the current image while retaining its view and video pairing.

        Progressive image loading can initially show a smaller decode. Keep the
        scene center as fractions of the old dimensions and keep zoom relative
        to fit, so substituting the full-resolution image does not move the
        subject. Callers should defer upgrades while a live clip is playing;
        this method cancels playback if called during one.
        """
        if image.isNull():
            return
        if self._image_rect.isEmpty():
            self.set_photo(image, self._video_path)
            return
        self.stop_live()
        relative_zoom = self.zoom_factor()
        center = self._scene_center()
        normalized = QPointF(
            center.x() / self._image_rect.width(),
            center.y() / self._image_rect.height(),
        )
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(1.0)
        self._photo.setPixmap(pixmap)
        self._image_rect = QRectF(0, 0, image.width(), image.height())
        self.setSceneRect(self._image_rect)
        self._video.setSize(QSizeF(image.width(), image.height()))
        self._fit_scale = self._calculate_fit_scale()
        scale = self._fit_scale * relative_zoom
        self.setTransform(QTransform.fromScale(scale, scale))
        self.centerOn(
            normalized.x() * image.width(), normalized.y() * image.height()
        )
        self._fit_mode = abs(relative_zoom - 1.0) < 0.001
        self.zoom_changed.emit(self.zoom_factor())

    def fit_image(self) -> None:
        self.stop_live()
        self._fit_view()

    def zoom_in(self) -> None:
        self.stop_live()
        self._zoom_at(1.25, self._viewport_center())

    def zoom_out(self) -> None:
        self.stop_live()
        self._zoom_at(1 / 1.25, self._viewport_center())

    def zoom_factor(self) -> float:
        return self.transform().m11() / self._fit_scale if self._fit_scale else 1.0

    def set_options(
        self,
        loop: bool = False,
        muted: bool = True,
        preserve_zoom: bool = True,
        hold_ms: int = 300,
    ) -> None:
        # A settings change is a new interaction, so a previous held input must
        # not continue playing underneath the settings dialog.
        self.stop_live()
        self._loop = bool(loop)
        self._preserve_zoom = bool(preserve_zoom)
        self._hold_ms = max(100, min(2000, int(hold_ms)))
        self._audio.setMuted(bool(muted))
        self._player.setLoops(QMediaPlayer.Loops.Infinite if loop else QMediaPlayer.Loops.Once)

    def start_live(self) -> None:
        """Play from the beginning, until release or the end of a single play."""
        if self._play_requested or self._image_rect.isEmpty() or self._video_path is None:
            return
        if not self._video_path.is_file():
            self.message.emit("视频文件不可用，请检查移动硬盘是否已连接。")
            return
        self._generation += 1
        generation = self._generation
        self._play_requested = True
        self._temporary_fit_applied = False
        self._saved_view = (
            (QTransform(self.transform()), self._scene_center(), self._fit_mode)
            if not self._preserve_zoom
            else None
        )
        self._video.hide()
        self._photo.show()
        # Clearing the sink prevents a frame left over from a previous hold from
        # being presented as the first frame of the new request.
        self._video.videoSink().setVideoFrame(QVideoFrame())
        if self._frame_slot is not None:
            self._video.videoSink().videoFrameChanged.disconnect(self._frame_slot)
        self._frame_slot = lambda frame, token=generation: self._video_frame(frame, token)
        self._video.videoSink().videoFrameChanged.connect(self._frame_slot)
        self._player.setLoops(
            QMediaPlayer.Loops.Infinite if self._loop else QMediaPlayer.Loops.Once
        )
        source = QUrl.fromLocalFile(str(self._video_path.resolve()))
        if self._player.source() != source:
            self._player.setSource(source)
        self._player.setPosition(0)
        self._load_timer.start()
        self._player.play()

    def stop_live(self) -> None:
        """Synchronously return to the photo and invalidate late media events."""
        self._hold_timer.stop()
        self._click_timer.stop()
        self._mouse_down = False
        self._mouse_moved = False
        self._hold_consumed = True
        self._touch_active = False
        self._touch_count = 0
        self._touch_had_multi = False
        self._stop_playback()
        self._release_capture()
        self.viewport().unsetCursor()

    def _stop_playback(self) -> None:
        self._generation += 1
        self._play_requested = False
        self._load_timer.stop()
        was_visible = self._live_visible
        self._live_visible = False
        # Switch immediately; QMediaPlayer.stop() may finish asynchronously.
        self._video.hide()
        self._photo.show()
        self._player.stop()
        if self._saved_view is not None:
            transform, center, was_fit = self._saved_view
            self._saved_view = None
            if self._temporary_fit_applied:
                self.setTransform(transform)
                self.centerOn(center)
                self._fit_mode = was_fit
                self.zoom_changed.emit(self.zoom_factor())
        self._temporary_fit_applied = False
        if was_visible:
            self.playback_changed.emit(False)

    def _video_frame(self, frame: QVideoFrame, generation: int) -> None:
        if generation != self._generation or not self._play_requested or not frame.isValid():
            return
        if not self._live_visible:
            self._load_timer.stop()
            if self._saved_view is not None:
                self._fit_view()
                self._temporary_fit_applied = True
            self._video.show()
            self._photo.hide()
            self._live_visible = True
            self.playback_changed.emit(True)

    def _media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if not self._play_requested:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._loop:
                # Qt normally handles infinite looping itself. This fallback
                # covers backends that surface EndOfMedia at the loop boundary.
                self._player.setPosition(0)
                self._player.play()
            else:
                self._hold_consumed = True
                self._hold_timer.stop()
                self._stop_playback()
                self._release_capture()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._play_failed("无法播放此视频，文件可能损坏或编码不受支持。")

    def _media_error(self, error: QMediaPlayer.Error, text: str) -> None:
        if error != QMediaPlayer.Error.NoError and self._play_requested:
            self._play_failed("视频播放失败：" + (text or "无法读取视频文件。"))

    def _load_timed_out(self) -> None:
        if self._play_requested and not self._live_visible:
            self._play_failed("视频加载时间较长，请检查硬盘连接后重试。")

    def _play_failed(self, text: str) -> None:
        self._hold_consumed = True
        self._hold_timer.stop()
        self._stop_playback()
        self._release_capture()
        self.message.emit(text)

    def _viewport_center(self) -> QPointF:
        return QPointF(self.viewport().rect().center())

    def _scene_center(self) -> QPointF:
        return self.mapToScene(self.viewport().rect().center())

    def _calculate_fit_scale(self) -> float:
        if self._image_rect.isEmpty():
            return 1.0
        return max(
            0.00001,
            min(
                max(1, self.viewport().width() - 4) / self._image_rect.width(),
                max(1, self.viewport().height() - 4) / self._image_rect.height(),
            ),
        )

    def _fit_view(self) -> None:
        self._fit_scale = self._calculate_fit_scale()
        self.setTransform(QTransform.fromScale(self._fit_scale, self._fit_scale))
        self.centerOn(self._image_rect.center())
        self._fit_mode = True
        self.zoom_changed.emit(1.0)

    def _zoom_at(self, multiplier: float, position: QPointF) -> None:
        if self._image_rect.isEmpty() or multiplier <= 0:
            return
        before = self.mapToScene(position.toPoint())
        old_scale = self.transform().m11()
        minimum = min(self._fit_scale, 1.0)
        maximum = max(self._fit_scale, 1.0) * 32.0
        scale = min(maximum, max(minimum, old_scale * multiplier))
        if abs(scale - self._fit_scale) < self._fit_scale * 0.015:
            scale = self._fit_scale
        self.setTransform(QTransform.fromScale(scale, scale))
        viewport_delta = self._viewport_center() - position
        self.centerOn(before + viewport_delta / scale)
        self._fit_mode = abs(scale - self._fit_scale) < self._fit_scale * 0.001
        self.zoom_changed.emit(self.zoom_factor())

    def _pan_by(self, delta: QPointF) -> None:
        if self._image_rect.isEmpty():
            return
        self.centerOn(self._scene_center() - delta / self.transform().m11())

    def _toggle_native_zoom(self, position: QPointF) -> None:
        self.stop_live()
        if self._fit_mode and abs(self.transform().m11() - 1.0) > 0.01:
            self._zoom_at(1.0 / self.transform().m11(), position)
        elif self._fit_mode:
            self._zoom_at(2.0, position)
        else:
            self._fit_view()

    def resizeEvent(self, event: QResizeEvent) -> None:
        old_center = self._scene_center()
        was_fit = self._fit_mode
        super().resizeEvent(event)
        if not hasattr(self, "_image_rect"):
            return
        self._fit_scale = self._calculate_fit_scale()
        if was_fit:
            self._fit_view()
        else:
            self.centerOn(old_center)
            self.zoom_changed.emit(self.zoom_factor())

    def wheelEvent(self, event: QWheelEvent) -> None:
        self.stop_live()
        amount = event.angleDelta().y()
        if not amount:
            amount = event.pixelDelta().y() * 3
        if amount:
            self._zoom_at(math.pow(1.0015, amount), event.position())
        event.accept()

    @staticmethod
    def _is_synthesized_mouse(event: QMouseEvent) -> bool:
        return event.source() != Qt.MouseEventSource.MouseEventNotSynthesized

    def _ignore_mouse(self, event: QMouseEvent) -> bool:
        return (
            self._touch_active
            or time.monotonic() < self._ignore_mouse_until
            or self._is_synthesized_mouse(event)
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._ignore_mouse(event):
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._stop_playback()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._mouse_down = True
        self._mouse_moved = False
        self._hold_consumed = False
        self._pointer_start = QPointF(event.position())
        self._pointer_last = QPointF(event.position())
        if self._video_path is not None:
            self._hold_timer.start(self._hold_ms)
        if self.isVisible():
            self.viewport().grabMouse()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._ignore_mouse(event):
            event.accept()
            return
        if self._mouse_down:
            position = event.position()
            if (position - self._pointer_start).manhattanLength() >= QApplication.startDragDistance():
                if not self._mouse_moved:
                    self._hold_timer.stop()
                    self._hold_consumed = True
                    self._stop_playback()
                self._mouse_moved = True
            if self._mouse_moved and not self._fit_mode:
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                self._pan_by(position - self._pointer_last)
            self._pointer_last = QPointF(position)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._ignore_mouse(event):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            was_click = self._mouse_down and not self._mouse_moved and not self._hold_consumed
            self._mouse_down = False
            self._hold_timer.stop()
            self._stop_playback()
            self._release_capture()
            self.viewport().unsetCursor()
            if was_click:
                self._click_timer.start(QApplication.doubleClickInterval())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._ignore_mouse(event):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            self._toggle_native_zoom(event.position())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _hold_timeout(self) -> None:
        if self._hold_consumed:
            return
        if (self._mouse_down and not self._mouse_moved) or (
            self._touch_active and self._touch_count == 1 and not self._touch_had_multi
        ):
            self._hold_consumed = True
            self.start_live()

    def _release_capture(self) -> None:
        if QWidget.mouseGrabber() is self.viewport():
            self._releasing_capture = True
            try:
                self.viewport().releaseMouse()
            finally:
                self._releasing_capture = False

    def viewportEvent(self, event: QEvent) -> bool:
        event_type = event.type()
        if event_type in (
            QEvent.Type.TouchBegin,
            QEvent.Type.TouchUpdate,
            QEvent.Type.TouchEnd,
            QEvent.Type.TouchCancel,
        ):
            self._touch_event(event)
            event.accept()
            return True
        if event_type == QEvent.Type.UngrabMouse and not self._releasing_capture:
            if self._mouse_down:
                self.stop_live()
        return super().viewportEvent(event)

    def _touch_event(self, event) -> None:
        event_type = event.type()
        self._ignore_mouse_until = time.monotonic() + 0.65
        if event_type == QEvent.Type.TouchCancel:
            self.stop_live()
            self._last_tap_time = 0.0
            return
        points = event.points()
        active = [p for p in points if p.state() != QEventPoint.State.Released]
        if event_type == QEvent.Type.TouchBegin:
            self.stop_live()
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._touch_active = True
            self._touch_had_multi = False
            self._touch_was_fit = self._fit_mode
            self._hold_consumed = False
            self._mouse_moved = False
            self._touch_count = len(active)
            if active:
                self._pointer_start = QPointF(active[0].position())
                self._pointer_last = QPointF(self._pointer_start)
            if len(active) == 1 and self._video_path is not None:
                self._hold_timer.start(self._hold_ms)
        if not self._touch_active:
            return
        if event_type == QEvent.Type.TouchEnd or not active:
            position = QPointF(points[0].position()) if points else self._pointer_last
            self._finish_touch(position)
            return
        previous_count = self._touch_count
        self._touch_count = len(active)
        if len(active) >= 2:
            self._hold_timer.stop()
            self._hold_consumed = True
            self._stop_playback()
            first, second = active[0].position(), active[1].position()
            center = (first + second) / 2
            distance = math.hypot(first.x() - second.x(), first.y() - second.y())
            if self._touch_had_multi and previous_count >= 2 and self._pinch_distance > 1:
                self._zoom_at(distance / self._pinch_distance, self._pinch_center)
                self._pan_by(center - self._pinch_center)
            self._touch_had_multi = True
            self._pinch_distance = distance
            self._pinch_center = QPointF(center)
            self._pointer_last = QPointF(first)
            return
        position = QPointF(active[0].position())
        if previous_count >= 2:
            # After a pinch, the remaining finger can pan, but it cannot start a
            # new long press or turn the same sequence into a swipe/tap.
            self._pointer_last = position
        elif (position - self._pointer_start).manhattanLength() >= QApplication.startDragDistance():
            self._hold_timer.stop()
            self._hold_consumed = True
            if not self._mouse_moved:
                self._stop_playback()
            self._mouse_moved = True
            if not self._fit_mode:
                self._pan_by(position - self._pointer_last)
        self._pointer_last = position

    def _finish_touch(self, position: QPointF) -> None:
        delta = position - self._pointer_start
        had_multi = self._touch_had_multi
        was_fit = self._touch_was_fit
        was_tap = not had_multi and not self._mouse_moved and not self._hold_consumed
        self._hold_timer.stop()
        self._stop_playback()
        self._touch_active = False
        self._touch_count = 0
        self._touch_had_multi = False
        self._release_capture()
        if (
            not had_multi
            and was_fit
            and self._mouse_moved
            and abs(delta.x()) >= max(60, self.viewport().width() * 0.08)
            and abs(delta.x()) > abs(delta.y()) * 1.35
        ):
            self.navigate_requested.emit(1 if delta.x() < 0 else -1)
        elif was_tap:
            now = time.monotonic()
            is_double = (
                now - self._last_tap_time <= QApplication.doubleClickInterval() / 1000
                and (position - self._last_tap_position).manhattanLength() < 36
            )
            if is_double:
                self._click_timer.stop()
                self._last_tap_time = 0.0
                self._toggle_native_zoom(position)
            else:
                self._last_tap_time = now
                self._last_tap_position = position
                self._click_timer.start(QApplication.doubleClickInterval())
        else:
            self._last_tap_time = 0.0

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self._touch_active or time.monotonic() < self._ignore_mouse_until:
            event.accept()
        else:
            super().contextMenuEvent(event)

    def focusOutEvent(self, event) -> None:
        self.stop_live()
        super().focusOutEvent(event)

    def leaveEvent(self, event) -> None:
        # A held pointer leaving the canvas must not leave media running when a
        # release is consumed by a system gesture or a different application.
        self.stop_live()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        self.stop_live()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        target = self.window()
        if self._window_filter_target is not target:
            if self._window_filter_target is not None:
                self._window_filter_target.removeEventFilter(self)
            self._window_filter_target = target
            target.installEventFilter(self)

    def eventFilter(self, watched, event: QEvent) -> bool:
        if watched is self._window_filter_target and event.type() in (
            QEvent.Type.WindowDeactivate,
            QEvent.Type.Hide,
            QEvent.Type.Close,
        ):
            self.stop_live()
        return super().eventFilter(watched, event)
