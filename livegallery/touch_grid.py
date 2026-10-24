"""A normal item view with explicit, bounded single-finger touch scrolling."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QItemSelectionModel, QPersistentModelIndex, QPointF, Qt
from PySide6.QtWidgets import QApplication, QListView


class OverviewGrid(QListView):
    """Keep taps and drags separate without synthesizing mouse clicks.

    Scrolling changes only the ordinary view scrollbar. The model, delegate and
    lazy thumbnail loading remain those of QListView, including for large
    libraries. Mouse and trackpad wheel interaction keep their normal behavior.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._touch_active = False
        self._touch_cancelled = False
        self._touch_moved = False
        self._touch_origin = QPointF()
        self._touch_scroll_origin = 0
        self._touch_index = QPersistentModelIndex()
        self._suppress_touch_mouse_until = 0.0

    def _end_touch(self):
        self._touch_active = False
        self._touch_index = QPersistentModelIndex()
        self._suppress_touch_mouse_until = time.monotonic() + .8

    def viewportEvent(self, event):
        kind = event.type()
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
            self._touch_cancelled = True
            self._end_touch()
            return True
        points = event.points()
        if kind == QEvent.Type.TouchBegin:
            self._touch_active = True
            self._touch_cancelled = len(points) != 1
            self._touch_moved = False
            self._touch_index = QPersistentModelIndex()
            self._suppress_touch_mouse_until = time.monotonic() + .8
            if points:
                self._touch_origin = QPointF(points[0].position())
                self._touch_scroll_origin = self.verticalScrollBar().value()
                self._touch_index = QPersistentModelIndex(self.indexAt(self._touch_origin.toPoint()))
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            return True
        if not self._touch_active:
            return True
        if len(points) != 1:
            self._touch_cancelled = True
        if points and not self._touch_cancelled:
            position = points[0].position()
            delta = position - self._touch_origin
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._touch_moved = True
            if self._touch_moved:
                self.verticalScrollBar().setValue(self._touch_scroll_origin - round(delta.y()))
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
        if self._touch_active:
            self._touch_cancelled = True
            self._end_touch()
        super().hideEvent(event)

    def focusOutEvent(self, event):
        if self._touch_active:
            self._touch_cancelled = True
            self._end_touch()
        super().focusOutEvent(event)
