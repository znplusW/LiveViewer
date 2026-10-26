"""Reserve an enabled downward top-edge swipe through its complete contact.

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
                 enabled_edges=('top',), edge_height=40, threshold=78):
        super().__init__(parent)
        self._windows_provider = windows_provider
        self._cancel_active = cancel_active
        # Reserve a finger-sized band: native touch contact centres can begin
        # a little inside the display even when the finger enters at its edge.
        self.edge_height = max(1, int(edge_height))
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
        self._retired = []  # Destroyed previews can deliver their last contact to the main window.
        self._retired_passthrough = []  # Explicit fresh ordinary contact + its promoted release.
        self._start = None
        self._cancelled = self._triggered = False
        self._tail_until = 0.0
        self._physical_right = 0.0
        self.native_policy = WindowEdgeGesturePolicy(self._windows, parent=self)
        self.set_enabled_edges(enabled_edges)
        self._app.installEventFilter(self)

    @property
    def enabled_edges(self):
        return self._enabled_edges

    def set_enabled_edges(self, edges):
        selected = frozenset(edge for edge in edges if edge == 'top')
        if selected != self._enabled_edges:
            self._enabled_edges = selected
            self._cancelled = True  # Never reinterpret a half-contact under new settings.
            self.native_policy.set_enabled(bool(selected))

    def close(self):
        if not self._closed:
            self._closed = True
            self._app.removeEventFilter(self)
            self.native_policy.close()
            self._reset()
            self._blocked.clear()
            self._retired.clear()
            self._retired_passthrough.clear()
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
        if self._device is not None and self._contacts:
            retired = next((entry for entry in self._retired if entry[0] == self._device), None)
            if retired is None:
                self._retired.append((self._device, set(self._contacts)))
            else:
                retired[1].update(self._contacts)
            self._forget_retired_passthrough(self._device)
        self._reset()

    def _forget_retired_passthrough(self, device):
        self._retired_passthrough = [entry for entry in self._retired_passthrough if entry['device'] != device]

    def _pass_unowned_press(self, window, device, point, new_press):
        # A real new press outside this gesture (including an exempt control)
        # must be able to use ordinary Qt mouse promotion. This does not mark
        # any retired ID as released. A later old tail re-arms its guard.
        if (new_press and window is not None and window.isVisible()
                and any(entry[0] == device for entry in self._retired)):
            position = window.mapFromGlobal(point.globalPosition())
            if 0 <= position.x() < window.width() and 0 <= position.y() < window.height():
                self._forget_retired_passthrough(device)
                self._retired_passthrough.append(dict(device=device, contact=point.id(), until=None))
        return False

    @staticmethod
    def _update_contacts(contacts, points):
        for point in points:
            if point.state() == QEventPoint.State.Released:
                contacts.discard(point.id())
            else:
                contacts.add(point.id())

    def _consume_owned(self, event, points=None):
        if points is None:
            points = event.points()
        kind = event.type()
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
                distance, sideways = delta.y(), abs(delta.x())
                matched = distance >= self.threshold and distance > sideways * 1.35
                possible = distance >= 0 and distance >= sideways / 1.35
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
            now = time.monotonic()
            for entry in self._retired_passthrough:
                if entry['device'] == device and any(point.id() == entry['contact']
                        and point.state() == QEventPoint.State.Released for point in points):
                    entry['until'] = now + .8
            retired = next((entry for entry in self._retired if entry[0] == device), None)
            if retired is not None:
                if kind == QEvent.Type.TouchBegin:
                    # Qt has begun a deliberate new sequence. A missing end
                    # from a destroyed HWND must never leave this device stuck.
                    self._retired.remove(retired)
                    self._forget_retired_passthrough(device)
                else:
                    if kind == QEvent.Type.TouchCancel:
                        retired[1].clear()
                        self._retired.remove(retired)
                        self._forget_retired_passthrough(device)
                        if active and device == self._device:
                            return self._consume_owned(event)
                        self._tail_until = now + .8
                        event.accept()
                        return True
                    old_ids = set(retired[1])
                    old_points = [point for point in points if point.id() in old_ids]
                    if old_points:
                        resumed = any(entry['device'] == device for entry in self._retired_passthrough)
                        self._forget_retired_passthrough(device)
                        if resumed and self._cancel_active is not None:
                            self._cancel_active()
                        if active and device == self._device:
                            # A delayed old contact must not become part of the
                            # recovered owner's set and make it wait for a lost
                            # End again. A still-held old finger cancels it.
                            self._update_contacts(retired[1], old_points)
                            if any(point.state() != QEventPoint.State.Released for point in old_points):
                                self._cancelled = True
                            if not retired[1]:
                                self._retired.remove(retired)
                            return self._consume_owned(event, [point for point in points if point.id() not in old_ids])
                        # Old + new points in one frame are a continuing
                        # multi-contact group; keep its whole tail reserved.
                        self._update_contacts(retired[1], points)
                        if not retired[1]:
                            self._retired.remove(retired)
                        self._tail_until = now + .8
                        event.accept()
                        return True
                    # A distinct Pressed ID is a new contact even when Qt's
                    # stale window group labels the event TouchUpdate. Keep
                    # quarantining old IDs; do not retire the whole device.
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
            candidates = [point for point in points if point.state() != QEventPoint.State.Released]
            new_press = (kind == QEvent.Type.TouchUpdate and len(points) == 1 and len(candidates) == 1
                         and candidates[0].state() == QEventPoint.State.Pressed)
            if kind != QEvent.Type.TouchBegin and not new_press:
                return False
            # A deliberate new sequence restores ordinary input immediately.
            self._tail_until = 0.0
            if (not self._enabled_edges or window is None or not window.isFullScreen() or not window.isVisible()
                    or len(candidates) != 1):
                return self._pass_unowned_press(window, device, candidates[0] if candidates else None, new_press)
            point = candidates[0]
            position = window.mapFromGlobal(point.globalPosition())
            eligible = set()
            if 0 <= position.y() < window.height() and 0 <= position.x() < window.width():
                if position.y() <= self.edge_height:
                    eligible.add('top')
            eligible.intersection_update(self._enabled_edges)
            if not eligible:
                return self._pass_unowned_press(window, device, point, new_press)
            if self._interactive_control(window, position):
                return self._pass_unowned_press(window, device, point, new_press)
            self._forget_retired_passthrough(device)
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

        # A finger can remain still after its preview HWND has gone away.
        # Keep owning that contact until release/cancel or a new sequence;
        # only the promoted mouse tail after release has a time limit.
        now = time.monotonic()
        self._retired_passthrough = [entry for entry in self._retired_passthrough
                                    if entry['until'] is None or now < entry['until']]
        retired_guarded = any(not any(entry['device'] == device for entry in self._retired_passthrough)
                              for device, _ in self._retired)
        guarded = active or retired_guarded or now < self._tail_until
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
