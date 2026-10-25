"""Photo and held-to-play Live Photo canvas.

The photo and video share one scene coordinate system. A video is fitted inside
the original photo rectangle, so it follows the same pan and zoom without being
stretched when its aspect ratio differs. Touch input is handled on the viewport
instead of being translated into mouse events.
"""

from __future__ import annotations

import math
import time
import weakref
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import (
    QContextMenuEvent,
    QColor,
    QEventPoint,
    QImage,
    QInputDevice,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)
from shiboken6 import isValid

from .native_touch import TouchContextMenuGuard, install_image_touch_filter
from .ui_theme import tokens_for
from .zoom_navigator import ZoomNavigator


class ViewerCanvas(QGraphicsView):
    """Scrollable photo canvas; ``zoom_factor() == 1`` means fit-to-window.

    ``start_live`` / ``stop_live`` are also the keyboard press/release entry
    points. The main window owns the keyboard binding and should ignore key
    auto-repeat. ``playback_changed`` becomes true only after a valid video frame
    has arrived, while a slow or failed video leaves the original photo visible.
    """

    navigate_requested = Signal(int)
    zoom_changed = Signal(float)
    zoom_animation_changed = Signal(bool)
    playback_changed = Signal(bool)
    message = Signal(str)
    controls_toggled = Signal()
    view_changed = Signal()
    interaction_cancelled = Signal()
    remote_context_menu_requested = Signal(object, QPointF)
    _DIRECT_INPUT = object()

    def apply_theme(self, colors):
        self.setBackgroundBrush(QColor(colors['canvas']))
        if hasattr(self, 'zoom_navigator'):
            self.zoom_navigator.apply_theme(colors)
        self.viewport().update()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.apply_theme(tokens_for(self))
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        # A complete raster frame must replace the previous video in one paint.
        # Partial scene updates can otherwise leave horizontal strips from an
        # earlier frame, particularly with rotated/cropped HEVC on Windows.
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
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
        self._video = QGraphicsPixmapItem()
        self._video.setZValue(1)
        self._video.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._video.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._video.setVisible(False)
        self.scene().addItem(self._video)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(True)
        self._player.setAudioOutput(self._audio)
        self._video_sink = QVideoSink(self)
        self._player.setVideoSink(self._video_sink)
        self._player.mediaStatusChanged.connect(self._media_status_changed)
        self._player.errorOccurred.connect(self._media_error)
        self._last_presented_frame = QVideoFrame()
        # Poll the sink's newest frame, instead of queueing a Python callback
        # for every decoded frame. This bounds storage and avoids entering
        # Python on the decoder thread while player.stop() waits for it.
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(16)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.timeout.connect(self._present_latest_video_frame)
        self._generation = 0
        self._video_path: Path | None = None
        self._image_rect = QRectF()
        self._fit_scale = 1.0
        self._fit_mode = True
        self._zoom_animation = QVariantAnimation(self)
        self._zoom_animation.setDuration(220)
        self._zoom_animation.setStartValue(0.0)
        self._zoom_animation.setEndValue(1.0)
        self._zoom_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._zoom_animation.valueChanged.connect(self._animate_zoom_value)
        self._zoom_animation.finished.connect(self._zoom_animation_finished)
        self._zoom_target_factor: float | None = None
        self._zoom_start_factor = 1.0
        self._zoom_start_center = QPointF()
        self._zoom_anchor_scene = QPointF()
        self._zoom_anchor_viewport = QPointF()
        self._deferred_photo: QImage | None = None
        self._loop = False
        self._horizontal_scroll_mode = "navigate"
        self._horizontal_wheel_total = 0.0
        self._horizontal_wheel_unit: str | None = None
        self._horizontal_wheel_last = 0.0
        self._horizontal_wheel_navigation_time = -math.inf
        self._preserve_zoom = True
        self._hold_ms = 300
        self._play_requested = False
        self._play_once = False
        self._live_visible = False
        self._saved_view: tuple[QTransform, QPointF, bool] | None = None
        self._temporary_fit_applied = False

        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._hold_timeout)
        self._blank_hold_timer = QTimer(self)
        self._blank_hold_timer.setSingleShot(True)
        self._blank_hold_timer.timeout.connect(self._blank_hold_timeout)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(12_000)
        self._load_timer.timeout.connect(self._load_timed_out)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.controls_toggled)

        self._mouse_down = False
        self._mouse_moved = False
        self._mouse_double_drag = False
        self._mouse_event_source = Qt.MouseEventSource.MouseEventNotSynthesized
        self._mouse_input_device = None
        self._last_mouse_click_time = 0.0
        self._last_mouse_click_timestamp = 0
        self._last_mouse_click_device = None
        self._last_mouse_click_position = QPointF()
        self._pointer_start = QPointF()
        self._pointer_last = QPointF()
        self._hold_consumed = False
        self._releasing_capture = False
        self._touch_active = False
        self._touch_had_multi = False
        self._touch_double_drag = False
        self._touch_was_fit = True
        self._touch_count = 0
        self._touch_device = None
        self._touch_ids: set[int] = set()
        self._pinch_distance = 0.0
        self._pinch_center = QPointF()
        self._ignore_mouse_until = 0.0
        self._last_tap_time = 0.0
        self._last_tap_timestamp = 0
        self._last_tap_device = None
        self._last_tap_position = QPointF()
        self._window_filter_target: QWidget | None = None
        self._touch_started_on_image = False
        self._touch_context_cancelled = False
        self._blank_context_seen = False
        self._last_physical_right_press = 0.0
        self._input_owner = None
        self._last_input_source = None
        self._dispatch_owner = None
        self._preserve_input_reset = 0
        self._input_epoch = 0
        self._window_group = weakref.WeakSet()
        self._group_check_pending = False
        self._native_touch_filter = install_image_touch_filter(self)
        self.zoom_navigator = ZoomNavigator(self.viewport())
        self.zoom_navigator.apply_theme(tokens_for(self))
        self.zoom_navigator.interactionStarted.connect(self._begin_navigator_interaction)
        self.zoom_navigator.centerRequested.connect(self._navigate_from_thumbnail)
        self.zoom_navigator.installEventFilter(self)
        self.zoom_changed.connect(self._update_zoom_navigator)

    def set_window_group(self, windows) -> None:
        """Declare application-owned control windows, without activating one."""
        for window in self._window_group:
            if isValid(window) and window is not self._window_filter_target:
                window.removeEventFilter(self)
        QApplication.instance().removeEventFilter(self)
        self._window_group = weakref.WeakSet(window for window in windows if window is not None and isValid(window))
        for window in self._window_group:
            window.installEventFilter(self)
        if self._window_group:
            QApplication.instance().installEventFilter(self)

    def is_window_group_active(self) -> bool:
        return (bool(self._window_group) and QApplication.applicationState() == Qt.ApplicationState.ApplicationActive
                and QApplication.activeModalWidget() is None
                and QApplication.activeWindow() in self._window_group)

    def defer_group_focus_check(self) -> None:
        if not self._window_group:
            self.cancel_interaction()
        elif not self._group_check_pending:
            self._group_check_pending = True
            QTimer.singleShot(0, self._check_group_focus)

    def _check_group_focus(self):
        if not isValid(self):
            return
        self._group_check_pending = False
        if not self.is_window_group_active():
            self._cancel_zoom_animation()
            self.zoom_navigator.cancel_interaction()
            self.cancel_interaction()

    def cancel_interaction(self) -> None:
        self.stop_live()
        self.zoom_navigator.cancel_interaction()

    def _claim_input(self, owner) -> bool:
        if self._input_owner is not None and self._input_owner is not owner:
            return False
        if owner is not self._DIRECT_INPUT and self.zoom_navigator._dragging:
            return False
        if self._last_input_source is not owner:
            self._last_mouse_click_time = self._last_tap_time = 0.0
            self._last_mouse_click_timestamp = self._last_tap_timestamp = 0
            self._last_mouse_click_device = self._last_tap_device = None
        self._input_owner = self._last_input_source = owner
        return True

    def _release_input(self, owner) -> None:
        if self._input_owner is owner:
            self._input_owner = None

    @contextmanager
    def _remote_dispatch(self, owner):
        previous = self._dispatch_owner
        self._dispatch_owner = owner
        try:
            yield
        finally:
            self._dispatch_owner = previous

    def begin_remote_input(self, owner) -> bool:
        return self._claim_input(owner)

    def end_remote_input(self, owner) -> None:
        self._release_input(owner)

    def remote_touch(self, owner, event) -> bool:
        """Consume explicit mapped logical points; never send an OS event."""
        if self._input_owner is not owner:
            return False
        with self._remote_dispatch(owner):
            result = self._touch_event(event)
        if event.type() in (QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            self._release_input(owner)
        return result

    def remote_mouse(self, owner, event) -> bool:
        if self._input_owner is not owner:
            return False
        handlers = {QEvent.Type.MouseButtonPress: self.mousePressEvent,
                    QEvent.Type.MouseButtonDblClick: self.mouseDoubleClickEvent,
                    QEvent.Type.MouseMove: self.mouseMoveEvent,
                    QEvent.Type.MouseButtonRelease: self.mouseReleaseEvent}
        with self._remote_dispatch(owner):
            handlers[event.type()](event)
        return True

    def remote_wheel(self, owner, event) -> bool:
        if not self._claim_input(owner):
            return False
        with self._remote_dispatch(owner):
            self.wheelEvent(event)
        self._release_input(owner)
        return True

    def set_photo(self, image: QImage, video_path: Path | None = None) -> None:
        """Replace the current image, cancelling every pending playback request."""
        self._cancel_zoom_animation(apply_deferred=False)
        self._last_tap_time = 0.0
        self._last_tap_timestamp = 0
        self._last_tap_device = None
        self.zoom_navigator.cancel_interaction()
        self._reset_horizontal_wheel()
        self.stop_live()
        self._click_timer.stop()
        self._player.setSource(QUrl())
        self._video_sink.setVideoFrame(QVideoFrame())
        self._video_path = Path(video_path) if video_path else None
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(1.0)
        self._photo.setPixmap(pixmap)
        self._photo.setVisible(True)
        self._image_rect = QRectF(0, 0, image.width(), image.height())
        self.setSceneRect(self._image_rect)
        self._video.setPixmap(QPixmap())
        self._fit_view()

    def clear(self) -> None:
        self.set_photo(QImage())

    def replace_photo(self, image: QImage) -> None:
        """Upgrade the current image while retaining its view and video pairing.

        Progressive image loading can initially show a smaller decode. Keep the
        scene center as fractions of the old dimensions and keep zoom relative
        to fit, so substituting the full-resolution image does not move the
        subject. Upgrades arriving during a double-click zoom are retained until
        the animation ends, so its target and subject do not jump. Callers should
        defer upgrades while a live clip is playing; this method cancels playback
        if called during one.
        """
        if image.isNull():
            return
        if self.is_zoom_animating():
            self._deferred_photo = QImage(image)
            return
        if self._input_owner is not None:
            self.cancel_interaction()
        if self._image_rect.isEmpty():
            self.set_photo(image, self._video_path)
            return
        if self._play_requested:
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
        self._video.setPixmap(QPixmap())
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

    def is_zoom_animating(self) -> bool:
        return self._zoom_target_factor is not None

    def set_options(
        self,
        loop: bool = False,
        muted: bool = True,
        preserve_zoom: bool = True,
        hold_ms: int = 300,
        horizontal_scroll_mode: str = "navigate",
    ) -> None:
        # A settings change is a new interaction, so a previous held input must
        # not continue playing underneath the settings dialog.
        self.stop_live()
        self._loop = bool(loop)
        self._preserve_zoom = bool(preserve_zoom)
        self._hold_ms = max(100, min(2000, int(hold_ms)))
        mode = horizontal_scroll_mode if horizontal_scroll_mode in ("navigate", "pan") else "navigate"
        if mode != self._horizontal_scroll_mode:
            self._reset_horizontal_wheel()
            self._horizontal_wheel_navigation_time = -math.inf
        self._horizontal_scroll_mode = mode
        self._audio.setMuted(bool(muted))
        self._player.setLoops(QMediaPlayer.Loops.Infinite if loop else QMediaPlayer.Loops.Once)

    def start_live(self) -> None:
        """Play from the beginning, until release or the end of a single play."""
        if self._play_once:
            self.stop_live()
        self._start_playback(play_once=False)

    def play_live_once(self) -> None:
        """Start one complete play, independent of the held-input loop setting."""
        # A new explicit trigger starts again even if an earlier clip is active.
        self.stop_live()
        self._start_playback(play_once=True)

    def release_live(self) -> None:
        """Release a hold binding without cancelling a latched one-shot play."""
        if not self._play_once:
            self.stop_live()

    def _start_playback(self, *, play_once: bool) -> None:
        self._cancel_zoom_animation()
        if self._play_requested or self._image_rect.isEmpty() or self._video_path is None:
            return
        if not self._video_path.is_file():
            self.message.emit("视频文件不可用，请检查移动硬盘是否已连接。")
            return
        self._generation += 1
        self._play_requested = True
        self._play_once = play_once
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
        self._video_sink.setVideoFrame(QVideoFrame())
        self._last_presented_frame = QVideoFrame()
        self._player.setLoops(
            QMediaPlayer.Loops.Infinite if self._loop and not play_once else QMediaPlayer.Loops.Once
        )
        source = QUrl.fromLocalFile(str(self._video_path.resolve()))
        if self._player.source() != source:
            self._player.setSource(source)
        self._player.setPosition(0)
        self._load_timer.start()
        self._frame_timer.start()
        self._player.play()

    def stop_live(self) -> None:
        """Synchronously return to the photo and invalidate late media events."""
        preserve_owner = self._preserve_input_reset
        self._preserve_input_reset = 0  # Only this contact reset; nested app actions cancel normally.
        if not preserve_owner:
            self._input_owner = self._last_input_source = None
            self._input_epoch += 1
            self.interaction_cancelled.emit()
        self._hold_timer.stop()
        self._blank_hold_timer.stop()
        self._click_timer.stop()
        self._mouse_down = False
        self._mouse_moved = False
        self._mouse_double_drag = False
        self._last_mouse_click_time = 0.0
        self._last_mouse_click_timestamp = 0
        self._last_mouse_click_device = None
        self._mouse_input_device = None
        self._hold_consumed = True
        if self._touch_active:
            self._touch_context_cancelled = True
        self._touch_active = False
        self._touch_count = 0
        self._touch_had_multi = False
        self._touch_double_drag = False
        self._last_tap_time = 0.0
        self._last_tap_timestamp = 0
        self._last_tap_device = None
        self._stop_playback()
        self._release_capture()
        self.viewport().unsetCursor()

    def _stop_playback(self) -> None:
        self._generation += 1
        self._play_requested = False
        self._play_once = False
        self._frame_timer.stop()
        self._last_presented_frame = QVideoFrame()
        self._load_timer.stop()
        was_visible = self._live_visible
        self._live_visible = False
        # Switch immediately; QMediaPlayer.stop() may finish asynchronously.
        self._video.hide()
        self._video.setPixmap(QPixmap())
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

    def _present_latest_video_frame(self) -> None:
        """GUI-thread pull: skip old frames if painting temporarily falls behind."""
        if not self._play_requested:
            return
        frame = self._video_sink.videoFrame()
        if not frame.isValid() or frame == self._last_presented_frame:
            return
        self._last_presented_frame = frame
        self._video_frame(frame, self._generation)

    @staticmethod
    def _frame_image(frame: QVideoFrame) -> QImage:
        # toImage applies the surface's orientation, crop, pixel stride and
        # color conversion. Frame-level display transforms are additional and
        # are deliberately not applied by toImage (Qt QVideoFrame docs).
        image = frame.toImage()
        if image.isNull():
            return image
        rotation = frame.rotation().value
        if rotation:
            image = image.transformed(QTransform().rotate(rotation))
        if frame.mirrored():
            image = image.flipped(Qt.Orientation.Horizontal)
        # Detach from decoder-managed storage before its next frame reuses a
        # surface. QPixmap then owns a complete immutable CPU-rendered frame.
        return image.copy()

    def _video_frame(self, frame: QVideoFrame, generation: int) -> None:
        if generation != self._generation or not self._play_requested or not frame.isValid():
            return
        image = self._frame_image(frame)
        if image.isNull() or generation != self._generation or not self._play_requested:
            return
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(1.0)
        self._video.setPixmap(pixmap)
        scale = min(
            self._image_rect.width() / image.width(),
            self._image_rect.height() / image.height(),
        )
        self._video.setScale(scale)
        self._video.setPos(
            self._image_rect.center() - QPointF(image.width() * scale / 2, image.height() * scale / 2)
        )
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
            if self._loop and not self._play_once:
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
        self._cancel_zoom_animation()
        self._fit_scale = self._calculate_fit_scale()
        self.setTransform(QTransform.fromScale(self._fit_scale, self._fit_scale))
        self.centerOn(self._image_rect.center())
        self._fit_mode = True
        self.zoom_changed.emit(1.0)

    def _zoom_at(self, multiplier: float, position: QPointF) -> None:
        self._cancel_zoom_animation()
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
        self._cancel_zoom_animation()
        if self._image_rect.isEmpty():
            return
        self.centerOn(self._scene_center() - delta / self.transform().m11())
        self._update_zoom_navigator()

    def _begin_navigator_interaction(self):
        self.stop_live()
        self._last_tap_time = 0.0
        self._cancel_zoom_animation()

    def _navigate_from_thumbnail(self, center):
        self.centerOn(center)
        self._update_zoom_navigator()

    def _update_zoom_navigator(self, *_):
        if not hasattr(self, 'zoom_navigator'):
            return
        navigator = self.zoom_navigator
        navigator.set_photo(self._photo.pixmap(), self._image_rect)
        navigator.set_available_size(self.viewport().size())
        navigator.set_view(self.mapToScene(self.viewport().rect()).boundingRect())
        navigator.move(max(0, self.viewport().width() - navigator.width() - 12),
                       max(0, self.viewport().height() - navigator.height() - 12))
        navigator.setVisible(not self._image_rect.isEmpty() and self.zoom_factor() > 1.001
                             and self.viewport().width() >= 48 and self.viewport().height() >= 48)
        navigator.raise_()
        self.view_changed.emit()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._update_zoom_navigator()

    def _toggle_double_zoom(self, position: QPointF) -> None:
        self.stop_live()
        if self._image_rect.isEmpty():
            return
        # A second double-click can reverse an in-flight transition. Always use
        # the fit-relative target, independently of the image's pixel dimensions.
        previous_target = self._zoom_target_factor
        target = (2.0 if previous_target == 1.0 else 1.0) if previous_target is not None else (
            2.0 if self._fit_mode else 1.0
        )
        self._cancel_zoom_animation()
        self._zoom_start_factor = self.zoom_factor()
        self._zoom_start_center = self._scene_center()
        self._zoom_anchor_scene = self.mapToScene(position.toPoint())
        self._zoom_anchor_viewport = QPointF(position)
        self._zoom_target_factor = target
        self.zoom_animation_changed.emit(True)
        self._zoom_animation.start()

    def _animate_zoom_value(self, progress) -> None:
        target = self._zoom_target_factor
        if target is None or self._image_rect.isEmpty():
            return
        progress = float(progress)
        factor = self._zoom_start_factor + (target - self._zoom_start_factor) * progress
        scale = self._fit_scale * factor
        self.setTransform(QTransform.fromScale(scale, scale))
        if target == 2.0:
            # Keep the selected subject under the pointer throughout zoom-in.
            center = self._zoom_anchor_scene + (
                self._viewport_center() - self._zoom_anchor_viewport
            ) / scale
        else:
            center = self._zoom_start_center + (
                self._image_rect.center() - self._zoom_start_center
            ) * progress
        self.centerOn(center)
        self._fit_mode = abs(factor - 1.0) < 0.001
        self.zoom_changed.emit(factor)

    def _zoom_animation_finished(self) -> None:
        if self._zoom_target_factor is None:
            return
        # Ensure exact 1x/2x endpoints even if the final animation tick is late.
        self._animate_zoom_value(1.0)
        self._zoom_target_factor = None
        self._apply_deferred_photo()
        self.zoom_animation_changed.emit(False)

    def _apply_deferred_photo(self) -> None:
        image, self._deferred_photo = self._deferred_photo, None
        if image is not None:
            self.replace_photo(image)

    def _cancel_zoom_animation(self, *, apply_deferred: bool = True) -> None:
        if not hasattr(self, "_zoom_animation"):
            return
        active = self.is_zoom_animating()
        self._zoom_animation.stop()
        self._zoom_target_factor = None
        if apply_deferred:
            self._apply_deferred_photo()
        else:
            self._deferred_photo = None
        if active:
            self.zoom_animation_changed.emit(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        if getattr(self, '_input_owner', None) is not None:
            self.cancel_interaction()
        self._cancel_zoom_animation()
        old_size = event.oldSize()
        if old_size.isValid():
            frame = 2 * self.frameWidth()
            old_point = QPointF(max(0, old_size.width() - frame - 1) // 2,
                                max(0, old_size.height() - frame - 1) // 2)
            old_center = self.mapToScene(old_point.toPoint())
        else:
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

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and getattr(self, '_input_owner', None) is not None:
            self.cancel_interaction()
        return super().event(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._input_owner is not None and self._input_owner is not (self._dispatch_owner or self._DIRECT_INPUT):
            event.accept()
            return
        phase = event.phase()
        if phase == Qt.ScrollPhase.ScrollBegin:
            self._reset_horizontal_wheel()
        pixels = event.pixelDelta()
        delta = pixels if not pixels.isNull() else event.angleDelta()
        if delta.x() and abs(delta.x()) > abs(delta.y()):
            self.stop_live()
            self._cancel_zoom_animation()
            self._horizontal_wheel(delta.x(), "pixel" if not pixels.isNull() else "angle")
        elif delta.y():
            self._reset_horizontal_wheel()
            self.stop_live()
            amount = delta.y() * 3 if not pixels.isNull() else delta.y()
            self._zoom_at(math.pow(1.0015, amount), event.position())
        if phase == Qt.ScrollPhase.ScrollEnd:
            self._reset_horizontal_wheel()
        event.accept()

    def _reset_horizontal_wheel(self) -> None:
        self._horizontal_wheel_total = 0.0
        self._horizontal_wheel_unit = None
        self._horizontal_wheel_last = 0.0

    def _horizontal_wheel(self, amount: float, unit: str) -> None:
        if self._image_rect.isEmpty():
            self._reset_horizontal_wheel()
            return
        if self._horizontal_scroll_mode == "pan" and self.zoom_factor() > 1.001:
            self._reset_horizontal_wheel()
            # Qt's positive X moves content right (scrolls toward the left).
            # At fit there is no horizontal pan: every setting navigates.
            pixels = amount if unit == "pixel" else amount / 120 * 60
            self._pan_by(QPointF(pixels, 0))
            return
        now = time.monotonic()
        if (unit != self._horizontal_wheel_unit
                or now - self._horizontal_wheel_last > 0.35
                or self._horizontal_wheel_total * amount < 0):
            self._horizontal_wheel_total = 0.0
        self._horizontal_wheel_unit = unit
        self._horizontal_wheel_last = now
        self._horizontal_wheel_total += amount
        threshold = 60 if unit == "pixel" else 120
        if abs(self._horizontal_wheel_total) < threshold:
            return
        # One action per event, at most every 180 ms. Discard excess momentum
        # rather than enqueueing page changes after the wheel has stopped.
        total, self._horizontal_wheel_total = self._horizontal_wheel_total, 0.0
        if now - self._horizontal_wheel_navigation_time < 0.18:
            return
        self._horizontal_wheel_navigation_time = now
        # Qt X is the opposite of WM_MOUSEHWHEEL: negative means scrolling
        # right, hence next. event.inverted() is informational; its OS setting
        # is already reflected in delta, so applying it again would undo it.
        self.navigate_requested.emit(1 if total < 0 else -1)

    @staticmethod
    def _is_synthesized_mouse(event: QMouseEvent) -> bool:
        return event.source() != Qt.MouseEventSource.MouseEventNotSynthesized

    def _ignore_mouse(self, event: QMouseEvent) -> bool:
        if self._touch_active or time.monotonic() < self._ignore_mouse_until:
            return True
        if not self._is_synthesized_mouse(event):
            return False
        # An explicitly promoted native double-click can own its following
        # system mouse tail. Qt-promoted events from our raw touch never do.
        return not (self._mouse_down and self._mouse_double_drag
                    and event.source() == self._mouse_event_source
                    and event.pointingDevice() == self._mouse_input_device)

    @staticmethod
    def _recent_contact(event, last_time, last_timestamp, last_device) -> bool:
        if last_time <= 0 or event.pointingDevice() != last_device:
            return False
        timestamp = event.timestamp()
        if timestamp > 0 and last_timestamp > 0 and timestamp >= last_timestamp:
            elapsed = timestamp - last_timestamp
        else:
            # Zero/unavailable or backwards clocks must never compare against
            # another device's epoch, nor create an unbounded repeat gesture.
            elapsed = (time.monotonic() - last_time) * 1000
        return 0 <= elapsed <= QApplication.doubleClickInterval()

    def _is_double_drag_press(self, event: QMouseEvent) -> bool:
        delta = event.position() - self._last_mouse_click_position
        hints = QApplication.styleHints()
        distance = hints.mouseDoubleClickDistance()
        if event.pointingDevice().type() == QInputDevice.DeviceType.TouchPad:
            distance = max(distance, hints.touchDoubleTapDistance())
        return (
            not self._fit_mode
            and self._recent_contact(event, self._last_mouse_click_time,
                                     self._last_mouse_click_timestamp, self._last_mouse_click_device)
            and max(abs(delta.x()), abs(delta.y())) <= distance
        )

    def _begin_mouse_press(self, event: QMouseEvent, *, double_drag: bool = False) -> None:
        position = event.position()
        self._stop_playback()
        self._hold_timer.stop()
        if self._dispatch_owner is None:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._mouse_down = True
        self._mouse_moved = False
        self._mouse_double_drag = double_drag
        self._mouse_event_source = event.source()
        self._mouse_input_device = event.pointingDevice()
        self._hold_consumed = double_drag
        self._pointer_start = QPointF(position)
        self._pointer_last = QPointF(position)
        if double_drag:
            # The second tap belongs to tap-to-drag. It can stay stationary
            # before the touchpad starts sending movement, beyond the Live
            # delay; it must never briefly acquire playback ownership.
            self._click_timer.stop()
            self._last_mouse_click_time = 0.0
            self._last_mouse_click_timestamp = 0
            self._last_mouse_click_device = None
        elif self._video_path is not None and self.image_contains_viewport_point(position):
            self._hold_timer.start(self._hold_ms)
        if self.isVisible() and self._dispatch_owner is None:
            self.viewport().grabMouse()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._input_owner is not None and self._input_owner is not (self._dispatch_owner or self._DIRECT_INPUT):
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton and not self._is_synthesized_mouse(event):
            # Real mouse right-click remains available even immediately after
            # touching an image. Windows touch-generated mouse events are
            # marked synthesized by Qt and do not take this route.
            self._last_physical_right_press = time.monotonic()
            super().mousePressEvent(event)
            return
        if self._ignore_mouse(event):
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if not self._claim_input(self._dispatch_owner or self._DIRECT_INPUT):
            event.accept()
            return
        self._begin_mouse_press(event, double_drag=self._is_double_drag_press(event))
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._input_owner is not None and self._input_owner is not (self._dispatch_owner or self._DIRECT_INPUT):
            event.accept()
            return
        if self._ignore_mouse(event):
            event.accept()
            return
        if self._mouse_down:
            position = event.position()
            if (position - self._pointer_start).manhattanLength() >= QApplication.startDragDistance():
                if not self._mouse_moved:
                    self._cancel_zoom_animation()
                    self._hold_timer.stop()
                    self._hold_consumed = True
                    self._stop_playback()
                self._mouse_moved = True
            if self._mouse_moved and not self._fit_mode:
                if self._dispatch_owner is None:
                    self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                self._pan_by(position - self._pointer_last)
            self._pointer_last = QPointF(position)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        owner = self._dispatch_owner or self._DIRECT_INPUT
        if self._input_owner is not None and self._input_owner is not owner:
            event.accept()
            return
        if self._ignore_mouse(event):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            owned_press = self._mouse_down
            double_drag = owned_press and self._mouse_double_drag
            was_click = owned_press and not self._mouse_moved and not self._hold_consumed
            self._mouse_down = False
            self._mouse_double_drag = False
            self._hold_timer.stop()
            # Switching from a mouse hold to the once shortcut releases this
            # pointer's ownership. Its eventual button-up cannot stop the new
            # latched play, nor can a stray release stop keyboard playback.
            if owned_press and not self._play_once:
                self._stop_playback()
            self._release_capture()
            self.viewport().unsetCursor()
            self._release_input(owner)
            if double_drag and not self._mouse_moved:
                # A completed second click zooms; moving while held pans at
                # the original scale instead. Decide only on button-up.
                self._toggle_double_zoom(event.position())
            elif was_click:
                self._last_mouse_click_time = time.monotonic()
                self._last_mouse_click_timestamp = event.timestamp()
                self._last_mouse_click_device = event.pointingDevice()
                self._last_mouse_click_position = QPointF(event.position())
                self._click_timer.start(QApplication.doubleClickInterval())
            else:
                self._last_mouse_click_time = 0.0
                self._last_mouse_click_timestamp = 0
                self._last_mouse_click_device = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        system_double = (event.source() == Qt.MouseEventSource.MouseEventSynthesizedBySystem
                         and event.pointingDevice().type() != QInputDevice.DeviceType.TouchScreen
                         and not self._touch_active and time.monotonic() >= self._ignore_mouse_until)
        if self._ignore_mouse(event) and not system_double:
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._claim_input(self._dispatch_owner or self._DIRECT_INPUT):
                event.accept()
                return
            self._click_timer.stop()
            if not self._fit_mode or self._mouse_double_drag:
                # The native DoubleClick itself identifies the second contact;
                # do not require a retained first click or a second spatial test.
                if not self._mouse_double_drag or event.pointingDevice() != self._mouse_input_device:
                    self._begin_mouse_press(event, double_drag=True)
                else:
                    self._mouse_event_source = event.source()
                event.accept()
                return
            self._toggle_double_zoom(event.position())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _hold_timeout(self) -> None:
        if self._hold_consumed:
            return
        if (self._mouse_down and not self._mouse_moved) or (
            self._touch_active and self._touch_started_on_image
            and self._touch_count == 1 and not self._touch_had_multi
        ):
            self._hold_consumed = True
            self.start_live()

    def _blank_hold_timeout(self) -> None:
        if (not self._touch_active or self._touch_started_on_image
                or self._touch_count != 1 or self._touch_had_multi
                or self._mouse_moved or self._blank_context_seen
                or self._image_rect.isEmpty()):
            return
        self._hold_consumed = True
        self._last_tap_time = 0.0
        position = self._pointer_start.toPoint()
        if self._input_owner is not None and self._input_owner is not self._DIRECT_INPUT:
            self._blank_context_seen = True
            self.remote_context_menu_requested.emit(self._input_owner, QPointF(position))
            return
        # Accepting TouchBegin is necessary to receive blank-area swipes. On
        # systems that then omit the promoted mouse context event, retain the
        # normal right-click action using the platform's press-and-hold timing.
        QApplication.sendEvent(self.viewport(), QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, position,
            self.viewport().mapToGlobal(position),
        ))

    def _release_capture(self) -> None:
        if QWidget.mouseGrabber() is self.viewport():
            self._releasing_capture = True
            try:
                self.viewport().releaseMouse()
            finally:
                self._releasing_capture = False

    def viewportEvent(self, event: QEvent) -> bool:
        event_type = event.type()
        context_guard = None
        if (event_type == QEvent.Type.ContextMenu and self._input_owner is not None
                and self._input_owner is not self._DIRECT_INPUT):
            event.accept()
            return True
        if event_type in (
            QEvent.Type.TouchBegin,
            QEvent.Type.TouchUpdate,
            QEvent.Type.TouchEnd,
            QEvent.Type.TouchCancel,
        ):
            if event_type == QEvent.Type.TouchBegin and not self._claim_input(self._DIRECT_INPUT):
                event.accept()
                return True
            if self._input_owner is not None and self._input_owner is not self._DIRECT_INPUT:
                event.accept()
                return True
            if self._touch_event(event):
                if event_type in (QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
                    self._release_input(self._DIRECT_INPUT)
                event.accept()
                return True
            # Empty canvas leaves all normal system gestures available.
            event.ignore()
            self._release_input(self._DIRECT_INPUT)
            return super().viewportEvent(event)
        if event_type == QEvent.Type.ContextMenu and self._suppress_touch_context(event):
            # This precedes QWidget's CustomContextMenu handling, which would
            # otherwise bypass contextMenuEvent and show the app's menu.
            event.accept()
            return True
        if event_type == QEvent.Type.ContextMenu and self._is_blank_touch_context(event):
            if self._blank_context_seen:
                event.accept()
                return True
            # Mark before emitting CustomContextMenu: the menu may run a nested
            # event loop and receive the Windows event after our fallback.
            self._blank_context_seen = True
            self._hold_consumed = True
            self._blank_hold_timer.stop()
            self._click_timer.stop()
            if self._touch_active and self._touch_device is not None:
                context_guard = TouchContextMenuGuard(self._touch_device, self._touch_ids)
        if event_type == QEvent.Type.UngrabMouse and not self._releasing_capture:
            if self._mouse_down:
                self.stop_live()
        if context_guard is not None:
            # CustomContextMenu invokes MainWindow's synchronous QMenu.exec.
            # Keep ownership of the opening touch across its nested event loop,
            # even when opening the menu cancels playback/focus on this canvas.
            with context_guard:
                return super().viewportEvent(event)
        return super().viewportEvent(event)

    def image_contains_viewport_point(self, position: QPointF) -> bool:
        if self._image_rect.isEmpty() or not self.viewport().rect().contains(position.toPoint()):
            return False
        return self._image_rect.contains(self.mapToScene(position.toPoint()))

    def _touch_event(self, event) -> bool:
        event_type = event.type()
        points = event.points()
        if event_type == QEvent.Type.TouchBegin:
            self._touch_started_on_image = bool(
                points and self.image_contains_viewport_point(points[0].position())
            )
            if self._image_rect.isEmpty():
                self.stop_live()
                self._ignore_mouse_until = 0.0
                return False
        if self._image_rect.isEmpty():
            return False
        self._ignore_mouse_until = time.monotonic() + 0.65
        if event_type == QEvent.Type.TouchCancel:
            self._blank_hold_timer.stop()
            self._touch_context_cancelled = True
            if self._touch_active and not self._play_once:
                self.stop_live()
            self._last_tap_time = 0.0
            return True
        active = [p for p in points if p.state() != QEventPoint.State.Released]
        if event_type == QEvent.Type.TouchBegin:
            previous_tap = (self._last_tap_time, self._last_tap_timestamp, self._last_tap_device)
            second_drag = False
            if len(active) == 1 and self._touch_started_on_image:
                # Recognize the second contact on finger-down in both fit and
                # zoomed views. Waiting until release used mouse-sized tap
                # tolerance and could start Live playback before deciding.
                delta = active[0].position() - self._last_tap_position
                distance = max(36, QApplication.styleHints().touchDoubleTapDistance())
                second_drag = (self._recent_contact(event, self._last_tap_time,
                                                    self._last_tap_timestamp, self._last_tap_device)
                               and max(abs(delta.x()), abs(delta.y())) <= distance)
            epoch = self._input_epoch
            self._preserve_input_reset = 1
            try:
                self.stop_live()
            finally:
                self._preserve_input_reset = 0
            if self._input_epoch != epoch:
                return True  # A synchronous playback callback replaced/cancelled the source.
            if not second_drag:
                # Keep the preceding tap only for an unrelated first contact;
                # a recognized second contact owns its entire touch sequence.
                self._last_tap_time, self._last_tap_timestamp, self._last_tap_device = previous_tap
            if self._dispatch_owner is None:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._touch_active = True
            self._touch_had_multi = False
            self._touch_double_drag = second_drag
            self._touch_context_cancelled = False
            self._blank_context_seen = False
            self._touch_was_fit = self._fit_mode
            self._hold_consumed = second_drag
            self._mouse_moved = False
            self._touch_count = len(active)
            self._touch_device = event.pointingDevice()
            self._touch_ids = {point.id() for point in active}
            if active:
                self._pointer_start = QPointF(active[0].position())
                self._pointer_last = QPointF(self._pointer_start)
            if len(active) == 1:
                if second_drag:
                    self._last_tap_time = 0.0
                    self._last_tap_timestamp = 0
                    self._last_tap_device = None
                elif self._touch_started_on_image and self._video_path is not None:
                    self._hold_timer.start(self._hold_ms)
                elif not self._touch_started_on_image:
                    self._blank_hold_timer.start(max(
                        1, QApplication.styleHints().mousePressAndHoldInterval()
                    ))
        if not self._touch_active:
            return True
        if event_type == QEvent.Type.TouchEnd or not active:
            position = QPointF(points[0].position()) if points else self._pointer_last
            self._finish_touch(position, event)
            return True
        previous_count = self._touch_count
        self._touch_count = len(active)
        if len(active) >= 2:
            self._cancel_zoom_animation()
            self._hold_timer.stop()
            self._blank_hold_timer.stop()
            self._touch_context_cancelled = True
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
            return True
        position = QPointF(active[0].position())
        if previous_count >= 2:
            # After a pinch, the remaining finger can pan, but it cannot start a
            # new long press or turn the same sequence into a swipe/tap.
            self._pointer_last = position
        elif max(abs(position.x() - self._pointer_start.x()),
                 abs(position.y() - self._pointer_start.y())) >= max(16, QApplication.startDragDistance()):
            # A finger's small diagonal jitter is not a mouse drag. Keep a
            # modest logical-pixel dead zone without weakening swipe/pan.
            self._cancel_zoom_animation()
            self._hold_timer.stop()
            self._blank_hold_timer.stop()
            self._touch_context_cancelled = True
            self._hold_consumed = True
            if not self._mouse_moved:
                self._stop_playback()
            self._mouse_moved = True
            if not self._fit_mode:
                self._pan_by(position - self._pointer_last)
        self._pointer_last = position
        return True

    def _finish_touch(self, position: QPointF, event) -> None:
        owned_press = self._touch_active
        delta = position - self._pointer_start
        had_multi = self._touch_had_multi
        double_drag = owned_press and self._touch_double_drag
        was_fit = self._touch_was_fit
        was_tap = (owned_press and self._touch_started_on_image
                   and not had_multi and not self._mouse_moved and not self._hold_consumed)
        self._hold_timer.stop()
        self._blank_hold_timer.stop()
        if owned_press and not self._play_once:
            self._stop_playback()
        self._touch_active = False
        self._touch_count = 0
        self._touch_had_multi = False
        self._touch_double_drag = False
        self._release_capture()
        if double_drag and not had_multi and not self._mouse_moved:
            self._last_tap_time = 0.0
            self._last_tap_timestamp = 0
            self._last_tap_device = None
            self._toggle_double_zoom(position)
        elif (
            owned_press
            and not had_multi
            and was_fit
            and self._mouse_moved
            and abs(delta.x()) >= max(60, self.viewport().width() * 0.08)
            and abs(delta.x()) > abs(delta.y()) * 1.35
        ):
            self.navigate_requested.emit(1 if delta.x() < 0 else -1)
        elif was_tap:
            self._last_tap_time = time.monotonic()
            self._last_tap_timestamp = event.timestamp()
            self._last_tap_device = event.pointingDevice()
            self._last_tap_position = position
            self._click_timer.start(QApplication.doubleClickInterval())
        else:
            self._last_tap_time = 0.0
            self._last_tap_timestamp = 0
            self._last_tap_device = None

    def _suppress_touch_context(self, event: QContextMenuEvent) -> bool:
        if event.reason() == QContextMenuEvent.Reason.Keyboard:
            return False
        if time.monotonic() - self._last_physical_right_press < 0.75:
            return False
        return (
            (self._touch_active or time.monotonic() < self._ignore_mouse_until)
            and (self._touch_context_cancelled or (
                self._touch_started_on_image
                and self.image_contains_viewport_point(QPointF(event.pos()))
            ))
        )

    def _is_blank_touch_context(self, event: QContextMenuEvent) -> bool:
        return (
            event.reason() != QContextMenuEvent.Reason.Keyboard
            and time.monotonic() - self._last_physical_right_press >= 0.75
            and not self._touch_started_on_image
            and (self._touch_active or time.monotonic() < self._ignore_mouse_until)
        )

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self._suppress_touch_context(event):
            event.accept()
        else:
            super().contextMenuEvent(event)

    def focusOutEvent(self, event) -> None:
        if self._window_group:
            self.defer_group_focus_check()
        else:
            self._cancel_zoom_animation()
            self.zoom_navigator.cancel_interaction()
            self.stop_live()
        super().focusOutEvent(event)

    def leaveEvent(self, event) -> None:
        # A held pointer leaving the canvas must not leave media running when a
        # release is consumed by a system gesture or a different application.
        remote = self._input_owner is not None and self._input_owner is not self._DIRECT_INPUT
        grouped_idle = self._window_group and not self._mouse_down and not self._touch_active
        if not self._play_once and not remote and not grouped_idle:
            self.stop_live()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        self._cancel_zoom_animation()
        self.stop_live()
        self.view_changed.emit()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        target = self.window()
        if self._window_filter_target is not target:
            if self._window_filter_target is not None and isValid(self._window_filter_target):
                if self._window_filter_target not in self._window_group:
                    self._window_filter_target.removeEventFilter(self)
            self._window_filter_target = target
            target.installEventFilter(self)
        self.view_changed.emit()

    def eventFilter(self, watched, event: QEvent) -> bool:
        if self._window_group:
            if event.type() == QEvent.Type.ApplicationDeactivate:
                self.cancel_interaction()
            elif event.type() == QEvent.Type.WindowDeactivate and watched in self._window_group:
                self.defer_group_focus_check()
            elif event.type() == QEvent.Type.Show and QApplication.activeModalWidget() is not None:
                self.defer_group_focus_check()
        if watched is self.zoom_navigator and self._input_owner is not None and self._input_owner is not self._DIRECT_INPUT:
            if event.type() in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd,
                                QEvent.Type.TouchCancel, QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                                QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
                event.accept()
                return True
        if watched is self._window_filter_target and event.type() in (
            QEvent.Type.WindowDeactivate,
            QEvent.Type.Hide,
            QEvent.Type.Close,
        ):
            if event.type() == QEvent.Type.WindowDeactivate and self._window_group:
                self.defer_group_focus_check()
            else:
                self.cancel_interaction()
        return super().eventFilter(watched, event)
