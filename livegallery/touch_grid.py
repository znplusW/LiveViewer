"""A lazy overview with single-finger scrolling and anchored pinch reflow."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, QItemSelectionModel, QPersistentModelIndex, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QEventPoint
from PySide6.QtWidgets import QApplication, QListView

from .catalog_model import (OVERVIEW_DEFAULT_WIDTH, OVERVIEW_MIN_WIDTH, OVERVIEW_MAX_WIDTH,
                            overview_tile_size)
from .filename_field import ElidedFilenameMouseHandler


class OverviewGrid(QListView):
    """Keep taps and drags separate without synthesizing mouse clicks.

    Scrolling changes only the ordinary view scrollbar. The model, delegate and
    lazy thumbnail loading remain those of QListView, including for large
    libraries. Mouse and trackpad wheel interaction keep their normal behavior.
    """

    thumbnail_size_changed = Signal(int)
    MIN_WIDTH = OVERVIEW_MIN_WIDTH
    MAX_WIDTH = OVERVIEW_MAX_WIDTH
    ZOOM_STEP = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._touch_active = False
        self._touch_device = None
        self._touch_contacts = set()
        self._touch_cancelled = False
        self._touch_moved = False
        self._touch_origin = QPointF()
        self._touch_scroll_origin = 0
        self._touch_index = QPersistentModelIndex()
        self._suppress_touch_mouse_until = 0.0
        self._thumbnail_width = OVERVIEW_DEFAULT_WIDTH
        self._size_applied = False
        self._touch_had_multi = False
        self._pinch_ids = ()
        self._pinch_distance = 0.0
        self._pinch_width = float(self._thumbnail_width)
        self._pinch_anchor = None
        self._filename_mouse = ElidedFilenameMouseHandler(self)

    @property
    def thumbnail_width(self):
        return self._thumbnail_width

    def _capture_anchor(self, point):
        point = QPointF(point)
        index = self.indexAt(point.toPoint())
        model = self.model()
        if not index.isValid() and model is not None and model.rowCount():
            # Gaps and the empty space after a short row also anchor to a
            # nearby photo. No model scan or image read is needed.
            cell = self.gridSize()
            if cell.isEmpty():
                cell = overview_tile_size(self._thumbnail_width) + QSize(self.spacing(), self.spacing())
            columns = max(1, self.viewport().width() // max(1, cell.width()))
            row = max(0, round(point.y()) + self.verticalScrollBar().value()) // max(1, cell.height())
            column = min(columns - 1, max(0, round(point.x())) // max(1, cell.width()))
            index = model.index(min(model.rowCount() - 1, row * columns + column), 0)
        if not index.isValid():
            return None
        rect = self.visualRect(index)
        fraction = min(1., max(0., (point.y() - rect.top()) / max(1, rect.height())))
        return QPersistentModelIndex(index), fraction, point.y()

    def _apply_thumbnail_width(self, width, anchor):
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, round(width)))
        changed = width != self._thumbnail_width
        if changed or not self._size_applied:
            self._thumbnail_width = width
            self._size_applied = True
            delegate = self.itemDelegate()
            if hasattr(delegate, 'set_thumbnail_width'):
                delegate.set_thumbnail_width(width)
            self.setGridSize(overview_tile_size(width) + QSize(self.spacing(), self.spacing()))
            self.doItemsLayout()
        if anchor is not None and anchor[0].isValid():
            index, fraction, target_y = anchor
            rect = self.visualRect(index)
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + round(rect.top() + fraction * rect.height() - target_y))
        if changed:
            self.thumbnail_size_changed.emit(width)
        self.viewport().update()
        return changed

    def set_thumbnail_width(self, width, anchor=None):
        """Resize existing cells while keeping a nearby photo at the same y."""
        point = self.viewport().rect().center() if anchor is None else anchor
        return self._apply_thumbnail_width(width, self._capture_anchor(point))

    def zoom_step(self, direction):
        return self.set_thumbnail_width(self.thumbnail_width + (self.ZOOM_STEP if direction > 0 else -self.ZOOM_STEP))

    def _pinch(self, points):
        first, second = sorted(points, key=lambda point: point.id())[:2]
        center = (first.position() + second.position()) / 2
        distance = math.hypot(first.position().x() - second.position().x(),
                              first.position().y() - second.position().y())
        ids = first.id(), second.id()
        self._touch_had_multi = True
        self._touch_index = QPersistentModelIndex()
        if ids != self._pinch_ids or self._pinch_distance <= 1:
            self._pinch_ids = ids
            self._pinch_width = float(self.thumbnail_width)
            self._pinch_anchor = self._capture_anchor(center)
        else:
            self._pinch_width = max(self.MIN_WIDTH, min(self.MAX_WIDTH,
                                    self._pinch_width * distance / self._pinch_distance))
            anchor = self._pinch_anchor
            if anchor is not None:
                anchor = anchor[0], anchor[1], center.y()
            self._apply_thumbnail_width(self._pinch_width, anchor)
        self._pinch_distance = distance

    def _end_touch(self):
        self._touch_active = False
        self._touch_device = None
        self._touch_contacts.clear()
        self._touch_index = QPersistentModelIndex()
        self._pinch_ids = ()
        self._pinch_distance = 0.0
        self._pinch_anchor = None
        self._suppress_touch_mouse_until = time.monotonic() + .8

    def viewportEvent(self, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove):
            if (event.source() != Qt.MouseEventSource.MouseEventNotSynthesized
                    and (self._touch_active or time.monotonic() < self._suppress_touch_mouse_until)):
                event.accept()
                return True
        filename_mouse = getattr(self, '_filename_mouse', None)
        if filename_mouse is not None and filename_mouse.handle(event):
            return True
        if kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                        QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return super().viewportEvent(event)

        event.accept()
        device = event.pointingDevice()
        if kind == QEvent.Type.TouchCancel:
            if not self._touch_active or device == self._touch_device:
                self._touch_cancelled = True
                self._end_touch()
            return True
        if self._touch_active and device != self._touch_device:
            return True
        points = event.points()
        if kind == QEvent.Type.TouchBegin and self._touch_active:
            self._end_touch()
        pressed = [point for point in points if point.state() == QEventPoint.State.Pressed]
        if not self._touch_active:
            # A fresh contact can arrive as TouchUpdate when another widget's
            # old contact still belongs to the Qt window. Only explicit presses
            # start our gesture; unknown updated/stationary tails are not ours.
            if not pressed:
                return True
            self._touch_active = True
            self._touch_device = device
            self._touch_contacts = {point.id() for point in pressed}
            self._touch_cancelled = False
            self._touch_had_multi = False
            self._touch_moved = False
            self._touch_index = QPersistentModelIndex()
            self._suppress_touch_mouse_until = time.monotonic() + .8
            if pressed:
                self._touch_origin = QPointF(pressed[0].position())
                self._touch_scroll_origin = self.verticalScrollBar().value()
                self._touch_index = QPersistentModelIndex(self.indexAt(self._touch_origin.toPoint()))
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            if self._touch_active and len(pressed) >= 2:
                self._pinch(pressed)
            return True
        self._touch_contacts.update(point.id() for point in pressed)
        points = [point for point in points if point.id() in self._touch_contacts]
        active = [point for point in points if point.state() != QEventPoint.State.Released]
        self._touch_contacts.difference_update(
            point.id() for point in points if point.state() == QEventPoint.State.Released)
        if len(active) >= 2 and not self._touch_cancelled:
            self._pinch(active)
            return True
        if self._touch_had_multi:
            # A remaining finger cannot turn the end of a pinch into a tap.
            if not self._touch_contacts:
                self._end_touch()
            return True
        if points and not self._touch_cancelled:
            position = points[0].position()
            delta = position - self._touch_origin
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._touch_moved = True
            if self._touch_moved:
                self.verticalScrollBar().setValue(self._touch_scroll_origin - round(delta.y()))
            if not self._touch_contacts:
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
        if not self._touch_contacts:
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
