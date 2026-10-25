"""Small decoded-photo overview and viewport positioning, without disk reads."""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QEventPoint, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget


class ZoomNavigator(QWidget):
    interactionStarted = Signal()
    centerRequested = Signal(QPointF)
    MAX_EDGE = 160

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('zoomNavigator')
        self.setAccessibleName('缩放导航')
        self.setToolTip('点击或拖动小图，定位放大后的画面')
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._thumbnail = QPixmap()
        self._photo_key = None
        self.thumbnail_revision = 0
        self._scene = QRectF()
        self._view = QRectF()
        self._image = QRectF()
        self._available = QSize(self.MAX_EDGE, self.MAX_EDGE)
        self._colors = {'surface': '#151a20', 'border': '#65758a', 'focus': '#9ac5ff'}
        self._dragging = False
        self._offset = QPointF()
        self._touch_active = False
        self._touch_cancelled = False
        self._touch_id = None
        self._touch_device = None
        self.hide()

    def apply_theme(self, colors):
        self._colors = dict(colors)
        self.update()

    def set_photo(self, pixmap, scene_rect):
        key = pixmap.cacheKey() if not pixmap.isNull() else None
        scene_rect = QRectF(scene_rect)
        if key == self._photo_key and scene_rect == self._scene:
            return
        if scene_rect != self._scene:
            self.cancel_interaction()
        self._photo_key, self._scene = key, scene_rect
        if key is None:
            self._thumbnail = QPixmap()
        else:
            # Retain only a bounded 2x thumbnail. Never retain another full image
            # or rebuild it while panning, zooming, or presenting video frames.
            self._thumbnail = pixmap.scaled(self.MAX_EDGE * 2, self.MAX_EDGE * 2,
                                            Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation)
            self._thumbnail.setDevicePixelRatio(1)
            self.thumbnail_revision += 1
        self._layout_image()
        self.update()

    def set_available_size(self, size):
        available = QSize(max(1, min(self.MAX_EDGE, size.width() - 24)),
                          max(1, min(self.MAX_EDGE, size.height() - 24)))
        if available != self._available:
            self._available = available
            self._layout_image()

    def _layout_image(self):
        if self._scene.isEmpty():
            self._image = QRectF()
            return
        width, height = self._available.width(), self._available.height()
        padding = min(6, (min(width, height) - 1) / 2)
        scale = min((width - 2 * padding) / self._scene.width(),
                    (height - 2 * padding) / self._scene.height())
        image_width, image_height = self._scene.width() * scale, self._scene.height() * scale
        size = QSize(min(width, math.ceil(image_width + padding * 2)),
                     min(height, math.ceil(image_height + padding * 2)))
        if size != self.size():
            self.setFixedSize(size)
        image = QRectF((size.width() - image_width) / 2, (size.height() - image_height) / 2,
                       image_width, image_height)
        if image != self._image:
            self._image = image
            self.update()

    def set_view(self, scene_rect):
        view = QRectF(scene_rect).intersected(self._scene)
        if view != self._view:
            self._view = view
            self.update()

    def image_rect(self):
        """Thumbnail rectangle in this widget's logical coordinates."""
        return QRectF(self._image)

    def visible_rect(self):
        """Current scene-clipped viewport mapped into the thumbnail."""
        if self._scene.isEmpty() or self._view.isEmpty():
            return QRectF()
        sx, sy = self._image.width() / self._scene.width(), self._image.height() / self._scene.height()
        return QRectF(self._image.left() + (self._view.left() - self._scene.left()) * sx,
                      self._image.top() + (self._view.top() - self._scene.top()) * sy,
                      self._view.width() * sx, self._view.height() * sy)

    def _scene_point(self, point):
        x = min(self._image.right(), max(self._image.left(), point.x()))
        y = min(self._image.bottom(), max(self._image.top(), point.y()))
        return QPointF(self._scene.left() + (x - self._image.left()) / self._image.width() * self._scene.width(),
                       self._scene.top() + (y - self._image.top()) / self._image.height() * self._scene.height())

    def _begin_drag(self, point):
        if self._image.isEmpty():
            return
        self.interactionStarted.emit()
        scene_point = self._scene_point(point)
        self._offset = scene_point - self._view.center() if self._view.contains(scene_point) else QPointF()
        self._dragging = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._request(point)

    def _request(self, point):
        if self._dragging and not self._image.isEmpty():
            self.centerRequested.emit(self._scene_point(point) - self._offset)

    def cancel_interaction(self):
        self._dragging = False
        self._touch_active = False
        self._touch_cancelled = True
        self._touch_id = None
        self._touch_device = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()

    def event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse):
            self.cancel_interaction()
        if kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                        QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return super().event(event)
        event.accept()
        if kind == QEvent.Type.TouchCancel:
            self.cancel_interaction()
            return True
        points = event.points()
        active = [point for point in points if point.state() != QEventPoint.State.Released]
        if kind == QEvent.Type.TouchBegin:
            self.cancel_interaction()
            if len(active) == 1:
                self._begin_drag(active[0].position())
                self._touch_active, self._touch_cancelled = True, False
                self._touch_id = active[0].id()
                self._touch_device = event.pointingDevice()
            return True
        if not self._touch_active:
            return True
        if event.pointingDevice() != self._touch_device:
            return True
        if len(points) != 1 or points[0].id() != self._touch_id:
            self._touch_cancelled = True
            self._dragging = False
        if points and not self._touch_cancelled:
            self._request(points[0].position())
        if kind == QEvent.Type.TouchEnd or not active:
            self.cancel_interaction()
        return True

    def mousePressEvent(self, event):
        event.accept()
        if event.source() != Qt.MouseEventSource.MouseEventNotSynthesized:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.cancel_interaction()
            self._begin_drag(event.position())
            if self._dragging:
                self.grabMouse()

    def mouseMoveEvent(self, event):
        event.accept()
        if event.source() == Qt.MouseEventSource.MouseEventNotSynthesized and not self._touch_active:
            self._request(event.position())

    def mouseReleaseEvent(self, event):
        event.accept()
        if event.source() == Qt.MouseEventSource.MouseEventNotSynthesized and event.button() == Qt.MouseButton.LeftButton:
            self._request(event.position())
            self.cancel_interaction()

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def wheelEvent(self, event):
        event.accept()

    def hideEvent(self, event):
        self.cancel_interaction()
        super().hideEvent(event)

    def paintEvent(self, event):
        if self._thumbnail.isNull() or self._image.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QPen(QColor(self._colors['border']), 1))
        painter.setBrush(QColor(self._colors['surface']))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5, 5)
        painter.drawPixmap(self._image, self._thumbnail, QRectF(self._thumbnail.rect()))
        visible = self.visible_rect()
        if visible.isEmpty():
            return
        outside, inside = QPainterPath(), QPainterPath()
        outside.addRect(self._image)
        inside.addRect(visible)
        painter.fillPath(outside.subtracted(inside), QColor(0, 0, 0, 85))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor('#ffffff'), 3))
        painter.drawRect(visible)
        painter.setPen(QPen(QColor(self._colors['focus']), 1.5))
        painter.drawRect(visible)
