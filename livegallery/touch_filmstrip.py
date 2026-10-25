"""Axis-aware filmstrip touch scrolling and photo-facing resize handles."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QItemSelectionModel, QPersistentModelIndex, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QListView, QWidget
from .ui_theme import tokens_for


class TouchFilmstrip(QListView):
    """Add touch scrolling without changing the model or mouse interaction."""

    viewportHeightChanged = Signal(int)
    viewportSizeChanged = Signal(QSize)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._scroll_axis = 'horizontal'
        self._drop_mouse_release = False
        self._touch_active = False
        self._touch_cancelled = False
        self._touch_moved = False
        self._touch_id = None
        self._touch_origin = QPointF()
        self._touch_scroll_origin = 0
        self._touch_index = QPersistentModelIndex()
        self._suppress_touch_mouse_until = 0.0
        self._viewport_height = self.viewport().height()
        self._viewport_size = self.viewport().size()

    @property
    def scroll_axis(self):
        return self._scroll_axis

    def current_scrollbar(self):
        return self.horizontalScrollBar() if self._scroll_axis == 'horizontal' else self.verticalScrollBar()

    def set_scroll_axis(self, axis):
        if axis not in ('horizontal', 'vertical'):
            raise ValueError('胶片条方向必须为 horizontal 或 vertical。')
        if axis != self._scroll_axis:
            self.cancel_gesture()
            self._scroll_axis = axis

    def cancel_gesture(self):
        self._cancel_touch()
        self._touch_cancelled = True
        self._drop_mouse_release = True
        self._suppress_touch_mouse_until = time.monotonic() + .8
        self.setState(QListView.State.NoState)

    def _notify_viewport_height(self):
        height = self.viewport().height()
        if height != getattr(self, '_viewport_height', None):
            self._viewport_height = height
            self.viewportHeightChanged.emit(height)
        size = self.viewport().size()
        if size != getattr(self, '_viewport_size', None):
            self._viewport_size = size
            self.viewportSizeChanged.emit(size)

    def _end_touch(self):
        self._touch_active = False
        self._touch_id = None
        self._touch_index = QPersistentModelIndex()
        self._suppress_touch_mouse_until = time.monotonic() + .8

    def _cancel_touch(self):
        if self._touch_active:
            self._touch_cancelled = True
            self._end_touch()

    def viewportEvent(self, event):
        kind = event.type()
        if kind == QEvent.Type.Resize:
            result = super().viewportEvent(event)
            self._notify_viewport_height()
            return result
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove):
            if (event.source() != Qt.MouseEventSource.MouseEventNotSynthesized
                    and (self._touch_active or time.monotonic() < self._suppress_touch_mouse_until)):
                event.accept()
                return True
            if kind == QEvent.Type.MouseButtonPress:
                self._drop_mouse_release = False
            elif self._drop_mouse_release and kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
                event.accept()
                return True
        if kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                        QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return super().viewportEvent(event)

        event.accept()
        if kind == QEvent.Type.TouchCancel:
            self._cancel_touch()
            return True
        points = event.points()
        if kind == QEvent.Type.TouchBegin:
            self._touch_active = True
            self._touch_cancelled = len(points) != 1
            self._touch_moved = False
            self._touch_index = QPersistentModelIndex()
            self._touch_id = points[0].id() if len(points) == 1 else None
            self._suppress_touch_mouse_until = time.monotonic() + .8
            if points:
                self._touch_origin = QPointF(points[0].position())
                self._touch_scroll_origin = self.current_scrollbar().value()
                self._touch_index = QPersistentModelIndex(self.indexAt(self._touch_origin.toPoint()))
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            return True
        if not self._touch_active:
            return True
        # A second finger permanently cancels this gesture's tap. Released
        # points can remain in an update, so never promote a remaining finger
        # into a fresh tap after a multi-finger sequence.
        if len(points) != 1 or points[0].id() != self._touch_id:
            self._touch_cancelled = True
        if points and not self._touch_cancelled:
            position = points[0].position()
            delta = position - self._touch_origin
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._touch_moved = True
            if self._touch_moved:
                amount = delta.x() if self._scroll_axis == 'horizontal' else delta.y()
                self.current_scrollbar().setValue(self._touch_scroll_origin - round(amount))
            if kind == QEvent.Type.TouchEnd:
                pressed = self._touch_index
                index = self.indexAt(position.toPoint())
                activate = (not self._touch_moved and pressed.isValid() and index.isValid()
                            and index == pressed and bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
                            and bool(index.flags() & Qt.ItemFlag.ItemIsSelectable))
                self._end_touch()
                if activate:
                    self.selectionModel().setCurrentIndex(
                        index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                    )
                    self.clicked.emit(index)
                return True
        if kind == QEvent.Type.TouchEnd:
            self._end_touch()
        return True

    def hideEvent(self, event):
        self.cancel_gesture()
        super().hideEvent(event)

    def focusOutEvent(self, event):
        self.cancel_gesture()
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._notify_viewport_height()


class FilmstripResizeHandle(QWidget):
    """Request thickness changes from the edge next to the photograph."""

    heightRequested = Signal(int)
    widthRequested = Signal(int)
    thicknessRequested = Signal(int)

    def __init__(self, film: QWidget):
        super().__init__(film)
        self.film = film
        self.edge = 'top'
        self.setObjectName('filmstripResizeHandle')
        self.setAccessibleName('调整胶片条高度')
        self.setToolTip('上下拖动，调整胶片条高度')
        self.setFixedHeight(14)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self._mouse_dragging = False
        self._touch_active = False
        self._touch_cancelled = False
        self._touch_id = None
        self._start_y = 0.0
        self._start_height = 0
        self._start_position = QPointF()
        self._start_thickness = 0
        self._last_requested = None
        self._suppress_touch_mouse_until = 0.0

    def set_edge(self, edge):
        if edge not in ('top', 'left', 'right'):
            raise ValueError('胶片条拖动边必须为 top、left 或 right。')
        self._cancel_drag()
        self.edge = edge
        if edge == 'top':
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.setFixedHeight(14)
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self.setAccessibleName('调整胶片条高度')
            self.setToolTip('上下拖动，调整胶片条高度')
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.setFixedWidth(14)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.setAccessibleName('调整胶片条宽度')
            self.setToolTip('左右拖动，调整胶片条宽度')
        self.update()

    def cancel_gesture(self):
        self._cancel_drag()

    def _begin_drag(self, position):
        self._start_position = QPointF(position)
        self._start_y = position.y()
        self._start_height = self.film.height()
        self._start_thickness = self.film.height() if self.edge == 'top' else self.film.width()
        self._last_requested = self._start_thickness
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def _request_thickness(self, position):
        delta = position - self._start_position
        difference = -delta.y() if self.edge == 'top' else delta.x() if self.edge == 'right' else -delta.x()
        thickness = self._start_thickness + round(difference)
        if thickness != self._last_requested:
            self._last_requested = thickness
            self.thicknessRequested.emit(thickness)
            if self.edge == 'top':
                self.heightRequested.emit(thickness)
            else:
                self.widthRequested.emit(thickness)

    def _end_touch(self):
        self._touch_active = False
        self._touch_id = None
        self._suppress_touch_mouse_until = time.monotonic() + .8
        self.update()

    def _cancel_drag(self):
        self._mouse_dragging = False
        self._touch_cancelled = True
        if self._touch_active:
            self._end_touch()
        self.update()

    def event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse):
            self._cancel_drag()
        if kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                        QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return super().event(event)
        event.accept()
        if kind == QEvent.Type.TouchCancel:
            self._cancel_drag()
            return True
        points = event.points()
        if kind == QEvent.Type.TouchBegin:
            self._mouse_dragging = False
            self._touch_active = True
            self._touch_cancelled = len(points) != 1
            self._touch_id = points[0].id() if len(points) == 1 else None
            self._suppress_touch_mouse_until = time.monotonic() + .8
            if not self._touch_cancelled:
                self._begin_drag(points[0].globalPosition())
            return True
        if not self._touch_active:
            return True
        if len(points) != 1 or points[0].id() != self._touch_id:
            self._touch_cancelled = True
            self.update()
        if points and not self._touch_cancelled:
            self._request_thickness(points[0].globalPosition())
        if kind == QEvent.Type.TouchEnd:
            self._end_touch()
        return True

    def _synthesized_mouse(self, event):
        return (event.source() != Qt.MouseEventSource.MouseEventNotSynthesized
                and (self._touch_active or time.monotonic() < self._suppress_touch_mouse_until))

    def mousePressEvent(self, event):
        if self._synthesized_mouse(event):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._mouse_dragging = True
            self._begin_drag(event.globalPosition())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._synthesized_mouse(event):
            event.accept()
            return
        if self._mouse_dragging:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._request_thickness(event.globalPosition())
                event.accept()
                return
            self._cancel_drag()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._synthesized_mouse(event):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._mouse_dragging:
            self._request_thickness(event.globalPosition())
            self._mouse_dragging = False
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        self._cancel_drag()
        super().hideEvent(event)

    def focusOutEvent(self, event):
        self._cancel_drag()
        super().focusOutEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = tokens_for(self)
        painter.setPen(QPen(QColor(colors['divider']), 1))
        if self.edge == 'top':
            middle = self.height() // 2
            painter.drawLine(0, middle, self.width(), middle)
        else:
            middle = self.width() // 2
            painter.drawLine(middle, 0, middle, self.height())
        active = self._mouse_dragging or (self._touch_active and not self._touch_cancelled)
        pen = QPen(QColor(colors['accent' if active else 'grip']), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.edge == 'top':
            center = self.width() // 2
            painter.drawLine(center - 20, middle, center + 20, middle)
        else:
            center = self.height() // 2
            painter.drawLine(middle, center - 20, middle, center + 20)
