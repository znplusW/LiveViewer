"""Reserve an enabled inward screen-edge swipe through its complete contact.

Qt handles recognition. A scoped HWND property requests fullscreen edge UI
suppression on Windows; it does not change global policy or verify hardware.
"""
from __future__ import annotations

import time
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QContextMenuEvent, QEventPoint
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QApplication,
                               QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QWidget)
from shiboken6 import isValid
from .zoom_navigator import ZoomNavigator
from .touch_filmstrip import FilmstripResizeHandle
from .native_fullscreen_gesture import WindowEdgeGesturePolicy


class FullscreenEdgeGesture(QObject):
    escapeRequested = Signal(object)

    def __init__(self, windows_provider, *, cancel_active=None, parent=None,
                 enabled_edges=('left',), edge_height=20, side_width=48, threshold=78):
        super().__init__(parent)
        self._windows_provider = windows_provider
        self._cancel_active = cancel_active
        self.edge_height = max(1, int(edge_height))
        # Real fingers often first reach Qt inside the side bezel. Keep the
        # working top band narrow while accepting a wider logical side start.
        self.side_width = max(1, int(side_width))
        self.threshold = max(1, int(threshold))
        self._enabled_edges = frozenset()
        self._eligible_edges = frozenset()
        self._app = QApplication.instance()
        self._closed = False
        self._target = None
        self._device = None
        self._contact = None
        self._contacts = set()
        self._blocked = []  # QPointingDevice has equality but is unhashable in PySide.
        self._start = None
        self._cancelled = self._triggered = False
        self._tail_until = 0.0
        self._physical_right = 0.0
        self.native_policy = WindowEdgeGesturePolicy(self._windows, parent=self)
        self.native_touch = None
        self.set_enabled_edges(enabled_edges)
        self._app.installEventFilter(self)
        from .native_edge_touch import install_native_edge_touch
        self.native_touch = install_native_edge_touch(self)

    @property
    def enabled_edges(self):
        return self._enabled_edges

    def set_enabled_edges(self, edges):
        selected = frozenset(edge for edge in edges if edge in {'top', 'left', 'right'})
        if selected != self._enabled_edges:
            self._enabled_edges = selected
            self._cancelled = True  # Never reinterpret a half-contact under new settings.
            if self.native_touch is not None:
                self.native_touch.cancel()
            self.native_policy.set_enabled(bool(selected))

    def close(self):
        if not self._closed:
            self._closed = True
            self._app.removeEventFilter(self)
            if self.native_touch is not None:
                self.native_touch.close()
            self.native_policy.close()
            self._reset()
            self._blocked.clear()
            self._windows_provider = lambda: ()
            self._cancel_active = None

    def _windows(self):
        return tuple(window for window in self._windows_provider()
                     if window is not None and isValid(window))

    def _event_window(self, receiver):
        for window in self._windows():
            if receiver is window or receiver is window.windowHandle():
                return window
            if isinstance(receiver, QWidget) and receiver.window() is window:
                return window
        return None

    @staticmethod
    def _interactive_control(window, position):
        child = window.childAt(position.toPoint())
        while child is not None and child is not window:
            if isinstance(child, (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox,
                                  QLineEdit, QTextEdit, QPlainTextEdit, ZoomNavigator,
                                  FilmstripResizeHandle)):
                return True
            child = child.parentWidget()
        return False

    def eligible_edges_at(self, window, position):
        """Accept the physical boundary, including subpixel rounding at a bezel."""
        if (not self._enabled_edges or not window.isFullScreen() or not window.isVisible()
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None):
            return frozenset()
        edges = set()
        if -1 <= position.y() <= window.height() and -1 <= position.x() <= window.width():
            if position.y() <= self.edge_height:
                edges.add('top')
            if position.x() <= self.side_width:
                edges.add('left')
            if position.x() >= window.width() - self.side_width:
                edges.add('right')
        edges.intersection_update(self._enabled_edges)
        return frozenset() if self._interactive_control(window, position) else frozenset(edges)

    def _reset(self):
        target = self._target() if self._target is not None else None
        if target is not None and isValid(target):
            target.destroyed.disconnect(self._target_destroyed)
        self._target = None
        self._device = None
        self._contact = None
        self._contacts.clear()
        self._start = None
        self._eligible_edges = frozenset()
        self._cancelled = self._triggered = False

    def _target_destroyed(self):
        self._tail_until = time.monotonic() + .8
        self._reset()

    @staticmethod
    def _update_contacts(contacts, points):
        for point in points:
            if point.state() == QEventPoint.State.Released:
                contacts.discard(point.id())
            else:
                contacts.add(point.id())

    def _consume_owned(self, event):
        points, kind = event.points(), event.type()
        if kind == QEvent.Type.TouchCancel:
            self._contacts.clear()
            self._cancelled = True
        else:
            self._update_contacts(self._contacts, points)
        if len(self._contacts) > 1:
            self._cancelled = True
        window = self._target() if self._target is not None else None
        if window is None or not isValid(window) or not window.isVisible():
            self._cancelled = True
        point = next((point for point in points if point.id() == self._contact), None)
        if point is not None and not self._cancelled and not self._triggered:
            if not window.isFullScreen():
                self._cancelled = True
            else:
                delta = window.mapFromGlobal(point.globalPosition()) - self._start
                directions = {'top': (delta.y(), abs(delta.x())),
                              'left': (delta.x(), abs(delta.y())),
                              'right': (-delta.x(), abs(delta.y()))}
                matched = any(distance >= self.threshold and distance > sideways * 1.35
                              for edge, (distance, sideways) in directions.items()
                              if edge in self._eligible_edges)
                possible = any(distance >= 0 and distance >= sideways / 1.35
                               for edge, (distance, sideways) in directions.items()
                               if edge in self._eligible_edges)
                if matched:
                    self._triggered = True
                    self.escapeRequested.emit(window)
                elif max(abs(delta.x()), abs(delta.y())) >= 30 and not possible:
                    self._cancelled = True
        # Exiting fullscreen may resize/hide/re-show the native window. Keep
        # owning the opening contact and its promoted tail after that change.
        self._tail_until = time.monotonic() + .8
        if not self._contacts:
            self._reset()
        event.accept()
        return True

    def eventFilter(self, receiver, event):
        if self._closed:
            return False
        kind = event.type()
        self.native_policy.observe(receiver, event)
        if self.native_touch is not None:
            if self.native_touch.observe(receiver, event):
                return True
        active = self._target is not None
        if active and kind in (QEvent.Type.ApplicationDeactivate, QEvent.Type.DevicePixelRatioChange):
            self._cancelled = True
        if active and kind in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.Resize):
            window = self._target()
            if receiver is window:
                self._cancelled = True
        if active and kind == QEvent.Type.Show and QApplication.activeModalWidget() is not None:
            self._cancelled = True

        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                    QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            device, points = event.pointingDevice(), event.points()
            blocked = next((entry for entry in self._blocked if entry[0] == device), None)
            if blocked is not None:
                if kind == QEvent.Type.TouchCancel:
                    blocked[1].clear()
                else:
                    self._update_contacts(blocked[1], points)
                if not blocked[1]:
                    self._blocked.remove(blocked)
                event.accept()
                return True
            window = self._event_window(receiver)
            if active:
                if device == self._device:
                    return self._consume_owned(event)
                if window is not None:
                    contacts = set()
                    self._update_contacts(contacts, points)
                    if contacts:
                        self._blocked.append((device, contacts))
                    event.accept()
                    return True
                return False
            if kind != QEvent.Type.TouchBegin:
                return False
            # A deliberate new sequence restores ordinary input immediately.
            self._tail_until = 0.0
            candidates = [point for point in points if point.state() != QEventPoint.State.Released]
            if (not self._enabled_edges or window is None or not window.isFullScreen() or not window.isVisible()
                    or len(candidates) != 1):
                return False
            point = candidates[0]
            position = window.mapFromGlobal(point.globalPosition())
            eligible = self.eligible_edges_at(window, position)
            if not eligible:
                return False
            self._target, self._device = weakref.ref(window), device
            window.destroyed.connect(self._target_destroyed)
            self._contact, self._contacts = point.id(), {point.id()}
            self._start = position
            self._eligible_edges = frozenset(eligible)
            self._cancelled = self._triggered = False
            if self._cancel_active is not None:
                self._cancel_active()
            self._tail_until = time.monotonic() + .8
            event.accept()
            return True

        guarded = active or time.monotonic() < self._tail_until
        if guarded and kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                                QEvent.Type.MouseMove, QEvent.Type.MouseButtonDblClick):
            if event.source() != Qt.MouseEventSource.MouseEventNotSynthesized:
                event.accept()
                return True
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
                self._physical_right = time.monotonic()
        if (guarded and kind == QEvent.Type.ContextMenu
                and event.reason() != QContextMenuEvent.Reason.Keyboard
                and time.monotonic() - self._physical_right >= .7):
            event.accept()
            return True
        return False
