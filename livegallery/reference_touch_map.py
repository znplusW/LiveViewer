"""Bounded static viewport reference and explicit indirect image input.

Only the existing decoded photo is sampled. The real ViewerCanvas owns the
scene, video decoder and gesture state; no OS pointer events are synthesized.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QEventPoint, QMouseEvent, QPainter, QPen,
                           QPixmap, QWheelEvent)
from PySide6.QtWidgets import QWidget

from .native_touch import TouchContextMenuGuard, install_image_touch_filter
from .ui_theme import tokens_for


class _MappedPoint:
    def __init__(self, point, position):
        self._id, self._state, self._position = point.id(), point.state(), QPointF(position)

    def id(self):
        return self._id

    def state(self):
        return self._state

    def position(self):
        return QPointF(self._position)


class _MappedTouch:
    def __init__(self, event, convert, kind=None):
        self._kind, self._device = kind or event.type(), event.pointingDevice()
        self._timestamp = event.timestamp()
        self._points = [_MappedPoint(point, convert(point.position())) for point in event.points()]

    def type(self):
        return self._kind

    def pointingDevice(self):
        return self._device

    def timestamp(self):
        return self._timestamp

    def points(self):
        return self._points


class ReferenceTouchMap(QWidget):
    contextMenuRequested = Signal(QPoint)  # Global location beside the map finger.
    MAX_THUMBNAIL_EDGE = 1024
    PADDING = 6

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.setObjectName('referenceTouchMap')
        self.setAccessibleName('预览触控区')
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMinimumSize(80, 80)
        self._colors = tokens_for(self)
        self._thumbnail = QPixmap()
        self._photo_key = None
        self.thumbnail_revision = 0
        self._active = self._mouse_active = self._navigation = False
        self._touch_device = None
        self._touch_ids = set()
        self._last_global = QPoint()
        self._nav_offset = QPointF()
        self._ignore_mouse_until = 0.0
        self._last_physical_right = 0.0
        self._menu_seen = False
        self._started_on_image = False
        viewer.view_changed.connect(self._refresh)
        viewer.interaction_cancelled.connect(self._cancel_local)
        viewer.remote_context_menu_requested.connect(self._remote_menu)
        self._native_touch_filter = install_image_touch_filter(self)
        self._refresh()

    def viewport(self):
        """The native image hold policy can use this widget as its viewport."""
        return self

    def apply_theme(self, colors):
        self._colors = dict(colors)
        self.update()

    def _refresh(self):
        pixmap = self.viewer._photo.pixmap()
        key = pixmap.cacheKey() if not pixmap.isNull() else None
        if key != self._photo_key:
            self._photo_key = key
            self._thumbnail = (QPixmap() if key is None else pixmap.scaled(
                min(self.MAX_THUMBNAIL_EDGE, pixmap.width()), min(self.MAX_THUMBNAIL_EDGE, pixmap.height()),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self._thumbnail.setDevicePixelRatio(1)
            self.thumbnail_revision += 1
        self.update()

    def mapped_rect(self):
        area = QRectF(self.rect()).adjusted(self.PADDING, self.PADDING, -self.PADDING, -self.PADDING)
        size = self.viewer.viewport().size()
        if area.isEmpty() or size.width() <= 0 or size.height() <= 0:
            return QRectF()
        scale = min(area.width() / size.width(), area.height() / size.height())
        width, height = size.width() * scale, size.height() * scale
        return QRectF(area.center().x() - width / 2, area.center().y() - height / 2, width, height)

    def map_to_viewer(self, point):
        area, size = self.mapped_rect(), self.viewer.viewport().size()
        if area.isEmpty():
            return QPointF()
        return QPointF((point.x() - area.left()) * size.width() / area.width(),
                       (point.y() - area.top()) * size.height() / area.height())

    def map_from_viewer(self, point):
        area, size = self.mapped_rect(), self.viewer.viewport().size()
        return QPointF(area.left() + point.x() * area.width() / max(1, size.width()),
                       area.top() + point.y() * area.height() / max(1, size.height()))

    def image_contains_viewport_point(self, position):
        return (self.mapped_rect().contains(position)
                and self.viewer.image_contains_viewport_point(self.map_to_viewer(position)))

    def navigator_rect(self):
        navigator = self.viewer.zoom_navigator
        if not navigator.isVisible():
            return QRectF()
        return QRectF(self.map_from_viewer(QPointF(navigator.pos())),
                      self.map_from_viewer(QPointF(navigator.geometry().bottomRight()) + QPointF(1, 1)))

    def _start(self, position):
        if (self._active or not self.viewer.isVisible() or self.viewer.window().isMinimized()
                or not self.mapped_rect().contains(position) or self._thumbnail.isNull()):
            return False
        if not self.viewer.begin_remote_input(self):
            return False
        self._active, self._menu_seen = True, False
        mapped = self.map_to_viewer(position)
        self._started_on_image = self.viewer.image_contains_viewport_point(mapped)
        self._navigation = self.navigator_rect().contains(position)
        if self._navigation:
            navigator = self.viewer.zoom_navigator
            self.viewer._hold_timer.stop()
            self.viewer._blank_hold_timer.stop()
            self.viewer._stop_playback()
            self.viewer._cancel_zoom_animation()
            point = navigator._scene_point(mapped - QPointF(navigator.pos()))
            self._nav_offset = point - navigator._view.center() if navigator._view.contains(point) else QPointF()
            self._navigate(position)
        return True

    def _navigate(self, position):
        navigator = self.viewer.zoom_navigator
        point = self.map_to_viewer(position) - QPointF(navigator.pos())
        self.viewer._navigate_from_thumbnail(navigator._scene_point(point) - self._nav_offset)

    def _finish(self):
        self.viewer.end_remote_input(self)
        self._cancel_local()

    def _cancel_local(self):
        self._active = self._mouse_active = self._navigation = False
        self._touch_device = None
        self._touch_ids.clear()
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()

    def cancel_interaction(self):
        if self.viewer._input_owner is self:
            self.viewer.cancel_interaction()
        self._cancel_local()

    def _remote_menu(self, owner, point):
        if owner is self and self._active:
            self._show_menu(self._last_global)

    def _show_menu(self, global_point):
        if self._menu_seen:
            return
        self._menu_seen = True
        device, ids = self._touch_device, set(self._touch_ids)
        if device is not None and ids:
            with TouchContextMenuGuard(device, ids):
                self.contextMenuRequested.emit(QPoint(global_point))
        else:
            self.contextMenuRequested.emit(QPoint(global_point))

    def contextMenuEvent(self, event):
        physical = time.monotonic() - self._last_physical_right < .7
        if not physical and (self._active or time.monotonic() < self._ignore_mouse_until):
            if self._started_on_image:
                event.accept()
                return
        if self.mapped_rect().contains(event.pos()):
            self._show_menu(event.globalPos())
        event.accept()

    def event(self, event):
        kind = event.type()
        if kind == QEvent.Type.DevicePixelRatioChange:
            self.cancel_interaction()
        if kind == QEvent.Type.UngrabMouse and self._mouse_active:
            self.cancel_interaction()
        if kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                        QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return super().event(event)
        event.accept()
        self._ignore_mouse_until = time.monotonic() + .7
        points = event.points()
        active = [point for point in points if point.state() != QEventPoint.State.Released]
        if kind == QEvent.Type.TouchBegin:
            if self._active:
                # A platform may begin delivery for an added point separately.
                # Retain the existing owner; it is a multi-touch update, never
                # another first contact with a fresh Live hold timer.
                if (event.pointingDevice() != self._touch_device
                        or not self._touch_ids.intersection(point.id() for point in active)):
                    return True
                kind = QEvent.Type.TouchUpdate
            else:
                if not active or not self._start(active[0].position()):
                    return True
                self._touch_device = event.pointingDevice()
        if not self._active or event.pointingDevice() != self._touch_device:
            return True
        self._touch_ids = {point.id() for point in active}
        if points:
            self._last_global = points[0].globalPosition().toPoint()
        if kind == QEvent.Type.TouchCancel:
            self.cancel_interaction()
        elif self._navigation:
            if len(points) != 1:
                self.cancel_interaction()
            elif points:
                self._navigate(points[0].position())
        else:
            self.viewer.remote_touch(self, _MappedTouch(event, self.map_to_viewer, kind))
        if kind == QEvent.Type.TouchEnd or not active:
            self._finish()
        return True

    def _mouse(self, event):
        event.accept()
        if (self._touch_device is not None or time.monotonic() < self._ignore_mouse_until
                or event.source() == Qt.MouseEventSource.MouseEventSynthesizedByQt):
            return
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != Qt.MouseButton.LeftButton:
                return
            if not self._active and not self._start(event.position()):
                return
            self._mouse_active = True
            self.grabMouse()
        if not self._active or not self._mouse_active:
            return
        self._last_global = event.globalPosition().toPoint()
        if self._navigation:
            self._navigate(event.position())
        else:
            point = self.map_to_viewer(event.position())
            mapped = QMouseEvent(kind, point, point, event.globalPosition(), event.button(),
                                 event.buttons(), event.modifiers(), event.source(), event.pointingDevice())
            mapped.setTimestamp(event.timestamp())
            self.viewer.remote_mouse(self, mapped)
        if kind == QEvent.Type.MouseButtonRelease:
            self._finish()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton and event.source() == Qt.MouseEventSource.MouseEventNotSynthesized:
            self._menu_seen = False
            self._last_physical_right = time.monotonic()
        self._mouse(event)

    def mouseDoubleClickEvent(self, event):
        self._mouse(event)

    def mouseMoveEvent(self, event):
        self._mouse(event)

    def mouseReleaseEvent(self, event):
        self._mouse(event)

    def wheelEvent(self, event):
        if (not self._active and self.viewer.isVisible() and not self.viewer.window().isMinimized()
                and self.mapped_rect().contains(event.position())):
            point = self.map_to_viewer(event.position())
            mapped = QWheelEvent(point, event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                                 event.buttons(), event.modifiers(), event.phase(), event.inverted(), event.source())
            self.viewer.remote_wheel(self, mapped)
        event.accept()

    def resizeEvent(self, event):
        self.cancel_interaction()
        super().resizeEvent(event)

    def hideEvent(self, event):
        self.cancel_interaction()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(self._colors['surface']))
        area = self.mapped_rect()
        painter.fillRect(area, self.viewer.backgroundBrush())
        if self._thumbnail.isNull() or area.isEmpty() or not self.viewer.isVisible():
            return
        image = self.viewer.viewportTransform().mapRect(self.viewer._image_rect)
        destination = QRectF(self.map_from_viewer(image.topLeft()), self.map_from_viewer(image.bottomRight()))
        painter.save()
        painter.setClipRect(area)
        painter.drawPixmap(destination, self._thumbnail, QRectF(self._thumbnail.rect()))
        navigator = self.viewer.zoom_navigator
        if navigator.isVisible():
            target = self.navigator_rect()
            painter.save()
            painter.translate(target.topLeft())
            painter.scale(target.width() / navigator.width(), target.height() / navigator.height())
            painter.fillRect(QRectF(navigator.rect()), QColor(self._colors['surface']))
            painter.drawPixmap(navigator.image_rect(), navigator._thumbnail, QRectF(navigator._thumbnail.rect()))
            painter.setPen(QPen(QColor('#ffffff'), 3))
            painter.drawRect(navigator.visible_rect())
            painter.setPen(QPen(QColor(self._colors['focus']), 1))
            painter.drawRect(navigator.visible_rect())
            painter.restore()
        painter.restore()
        painter.setPen(QPen(QColor(self._colors['border']), 1))
        painter.drawRect(area.adjusted(.5, .5, -.5, -.5))
