"""Filmstrip taps and single-finger horizontal scrolling on the existing view."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QItemSelectionModel, QPersistentModelIndex, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QListView, QWidget
from .ui_theme import tokens_for


class TouchFilmstrip(QListView):
    """Add touch scrolling without changing the model or mouse interaction."""

    viewportHeightChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._touch_active = False
        self._touch_cancelled = False
        self._touch_moved = False
        self._touch_id = None
        self._touch_origin = QPointF()
        self._touch_scroll_origin = 0
        self._touch_index = QPersistentModelIndex()
        self._suppress_touch_mouse_until = 0.0
        self._viewport_height = self.viewport().height()

    def _notify_viewport_height(self):
        height = self.viewport().height()
        if height != getattr(self, '_viewport_height', None):
            self._viewport_height = height
            self.viewportHeightChanged.emit(height)

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
                self._touch_scroll_origin = self.horizontalScrollBar().value()
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
                self.horizontalScrollBar().setValue(self._touch_scroll_origin - round(delta.x()))
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
        self._cancel_touch()
        super().hideEvent(event)

    def focusOutEvent(self, event):
        self._cancel_touch()
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._notify_viewport_height()


class FilmstripResizeHandle(QWidget):
    """Request film height changes using stable screen coordinates."""

    heightRequested = Signal(int)

    def __init__(self, film: QWidget):
        super().__init__(film)
        self.film = film
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
        self._last_requested = None
        self._suppress_touch_mouse_until = 0.0

    def _begin_drag(self, global_y):
        self._start_y = global_y
        self._start_height = self.film.height()
        self._last_requested = self._start_height
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def _request_height(self, global_y):
        height = self._start_height - round(global_y - self._start_y)
        if height != self._last_requested:
            self._last_requested = height
            self.heightRequested.emit(height)

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
                self._begin_drag(points[0].globalPosition().y())
            return True
        if not self._touch_active:
            return True
        if len(points) != 1 or points[0].id() != self._touch_id:
            self._touch_cancelled = True
            self.update()
        if points and not self._touch_cancelled:
            self._request_height(points[0].globalPosition().y())
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
            self._begin_drag(event.globalPosition().y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._synthesized_mouse(event):
            event.accept()
            return
        if self._mouse_dragging:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._request_height(event.globalPosition().y())
                event.accept()
                return
            self._cancel_drag()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._synthesized_mouse(event):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._mouse_dragging:
            self._request_height(event.globalPosition().y())
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
        middle = self.height() // 2
        colors = tokens_for(self)
        painter.setPen(QPen(QColor(colors['divider']), 1))
        painter.drawLine(0, middle, self.width(), middle)
        active = self._mouse_dragging or (self._touch_active and not self._touch_cancelled)
        pen = QPen(QColor(colors['accent' if active else 'grip']), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center = self.width() // 2
        painter.drawLine(center - 20, middle, center + 20, middle)
