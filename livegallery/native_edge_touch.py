"""Receive bezel contacts before Qt's widget targeting / touch translation.

Only enabled fullscreen edge sequences are owned, from DOWN (or NEW ENTER)
through UP/capture loss. Other pointer streams stay entirely with Qt. This
cannot recover input withheld by the OS or the touchscreen firmware.
https://learn.microsoft.com/windows/win32/inputmsg/wm-pointerdown
https://learn.microsoft.com/windows/win32/api/winuser/ns-winuser-pointer_info
"""
from __future__ import annotations

import ctypes
import sys
import time
import weakref

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QPointF, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from .native_touch import _Message, _Point

DOWN, UPDATE, UP, ENTER, LEAVE, CAPTURE_CHANGED = 0x246, 0x245, 0x247, 0x249, 0x24A, 0x24C
NONCLIENT_MESSAGES = {0x242: DOWN, 0x241: UPDATE, 0x243: UP}
NEW, INCONTACT, CANCELED = 0x1, 0x4, 0x8000
PT_TOUCH = 2


class PointerInfo(ctypes.Structure):
    _fields_ = [('pointerType', ctypes.c_uint32), ('pointerId', ctypes.c_uint32),
                ('frameId', ctypes.c_uint32), ('pointerFlags', ctypes.c_uint32),
                ('sourceDevice', ctypes.c_void_p), ('hwndTarget', ctypes.c_void_p),
                ('ptPixelLocation', _Point), ('ptHimetricLocation', _Point),
                ('ptPixelLocationRaw', _Point), ('ptHimetricLocationRaw', _Point),
                ('dwTime', ctypes.c_uint32), ('historyCount', ctypes.c_uint32),
                ('InputData', ctypes.c_int32), ('dwKeyStates', ctypes.c_uint32),
                ('PerformanceCount', ctypes.c_uint64), ('ButtonChangeType', ctypes.c_uint32)]


class PointerBackend:
    def __init__(self):
        self.user = ctypes.WinDLL('user32.dll', use_last_error=True, winmode=0x800)
        for name, args, result in (
            ('GetPointerInfo', [ctypes.c_uint32, ctypes.POINTER(PointerInfo)], ctypes.c_int),
            ('GetPointerFrameInfo', [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                     ctypes.POINTER(PointerInfo)], ctypes.c_int),
            ('GetAncestor', [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p),
            ('ScreenToClient', [ctypes.c_void_p, ctypes.POINTER(_Point)], ctypes.c_int)):
            method = getattr(self.user, name)
            method.argtypes, method.restype = args, result

    def info(self, pointer):
        value = PointerInfo()
        return value if self.user.GetPointerInfo(pointer, ctypes.byref(value)) else None

    def contacts(self, pointer):
        count = ctypes.c_uint32()
        if not self.user.GetPointerFrameInfo(pointer, ctypes.byref(count), None) or not 0 < count.value <= 256:
            return None
        values = (PointerInfo * count.value)()
        if not self.user.GetPointerFrameInfo(pointer, ctypes.byref(count), values):
            return None
        return sum(p.pointerType == PT_TOUCH and bool(p.pointerFlags & INCONTACT) for p in values[:count.value])

    def root(self, hwnd):
        return self.user.GetAncestor(hwnd, 2)

    def position(self, window, info):
        # Raw coordinates avoid Windows motion prediction pushing the first
        # contact away from the bezel. ScreenToClient handles negative monitors.
        point = _Point(info.ptPixelLocationRaw.x, info.ptPixelLocationRaw.y)
        if not self.user.ScreenToClient(int(window.winId()), ctypes.byref(point)):
            return None
        scale = window.devicePixelRatioF()
        return QPointF(point.x / scale, point.y / scale)


class NativeEdgeTouch(QAbstractNativeEventFilter):
    def __init__(self, owner, backend=None):
        super().__init__()
        self.owner = weakref.ref(owner)
        self.backend = backend or PointerBackend()
        self.window = None
        self.ids = set()
        self.primary = None
        self.start = None
        self.edges = frozenset()
        self.cancelled = self.triggered = False
        self.closed = False
        self.claims = self.exits = 0

    def target(self):
        target = self.window() if self.window else None
        return target if target is not None and isValid(target) else None

    def cancel(self):
        self.cancelled = True

    def reset(self):
        self.window = None
        self.ids.clear()
        self.primary = self.start = None
        self.edges = frozenset()
        self.cancelled = self.triggered = False

    def close(self):
        if not self.closed:
            self.closed = True
            QApplication.instance().removeNativeEventFilter(self)
            self.reset()

    def observe(self, receiver, event):
        if not self.ids:
            return False
        owner, target, kind = self.owner(), self.target(), event.type()
        if target is None:
            self.reset()
            return False
        if (kind in (QEvent.Type.ApplicationDeactivate, QEvent.Type.DevicePixelRatioChange)
                or receiver is target and kind in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.Resize)
                or kind == QEvent.Type.Show and QApplication.activeModalWidget() is not None):
            self.cancel()
        # The native stream is consumed, but do not let a duplicate Qt packet
        # start a second gesture. Ordinary non-edge Qt touch never enters here.
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            if owner is not None and owner._event_window(receiver) is target:
                event.accept()
                return True
        return False

    def handle(self, hwnd, kind, pointer):
        owner = self.owner()
        if self.closed or owner is None or owner._closed:
            return False
        if not self.ids and not owner.enabled_edges:
            return False
        owned = pointer in self.ids
        if kind == CAPTURE_CHANGED:
            if owned:
                self.cancel()
                self.ids.discard(pointer)
                owner._tail_until = time.monotonic() + .8
                if not self.ids:
                    self.reset()
            return owned
        info = self.backend.info(pointer)
        if info is None or info.pointerType != PT_TOUCH:
            if owned:
                self.cancel()
                if kind in (UP, LEAVE):
                    self.ids.discard(pointer)
                    if not self.ids:
                        self.reset()
            return owned
        target = self.target()
        root = self.backend.root(hwnd)
        window = next((w for w in owner._windows()
                       if w.testAttribute(Qt.WidgetAttribute.WA_WState_Created) and int(w.winId()) == root), None)
        if target is not None and kind == DOWN and not owned and window is not None:
            self.ids.add(pointer)
            self.cancel()
            owned = True
        if not self.ids:
            # Never steal an UPDATE from an ordinary swipe already handled by
            # Qt. NEW ENTER can precede DOWN at the exact screen boundary.
            if (window is None or not window.isFullScreen() or not window.isVisible() or owner._target is not None or
                    not (kind == DOWN or kind == ENTER and info.pointerFlags & NEW and info.pointerFlags & INCONTACT)
                    or info.pointerFlags & CANCELED or self.backend.contacts(pointer) != 1):
                return False
            position = self.backend.position(window, info)
            edges = owner.eligible_edges_at(window, position) if position is not None else frozenset()
            if not edges:
                return False
            self.window, self.start, self.edges = weakref.ref(window), position, edges
            self.ids, self.primary = {pointer}, pointer
            self.cancelled = self.triggered = False
            self.claims += 1
            if owner._cancel_active is not None:
                owner._cancel_active()
            target, owned = window, True
        if not owned:
            return False
        owner._tail_until = time.monotonic() + .8
        if (target is None or not target.isVisible() or not target.isFullScreen()
                or info.pointerFlags & CANCELED or self.backend.contacts(pointer) not in (0, 1)):
            self.cancel()
        position = self.backend.position(target, info) if target is not None else None
        if position is None:
            self.cancel()
        if pointer == self.primary and not self.cancelled and not self.triggered:
            delta = position - self.start
            directions = {'top': (delta.y(), abs(delta.x())),
                          'left': (delta.x(), abs(delta.y())),
                          'right': (-delta.x(), abs(delta.y()))}
            if any(d >= owner.threshold and d > cross * 1.35 for edge, (d, cross) in directions.items() if edge in self.edges):
                self.triggered = True
                self.exits += 1
                owner.escapeRequested.emit(target)
            elif max(abs(delta.x()), abs(delta.y())) >= 30 and not any(
                    d >= 0 and d >= cross / 1.35 for edge, (d, cross) in directions.items() if edge in self.edges):
                self.cancel()
        if kind == UP or kind == LEAVE and not info.pointerFlags & INCONTACT:
            self.ids.discard(pointer)
            if not self.ids:
                self.reset()
        return True

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) != b'windows_generic_MSG':
            return False, 0
        msg = _Message.from_address(int(message))
        # Windows may classify the outermost pixel as a non-client boundary.
        # Only the fullscreen eligibility gate may claim that contact.
        kind = NONCLIENT_MESSAGES.get(msg.message, msg.message)
        if kind not in (DOWN, UPDATE, UP, ENTER, LEAVE, CAPTURE_CHANGED):
            return False, 0
        return self.handle(msg.hwnd, kind, int(msg.wParam) & 0xffff), 0


def install_native_edge_touch(owner):
    if sys.platform != 'win32' or QApplication.instance().platformName() != 'windows':
        return None
    try:
        result = NativeEdgeTouch(owner)
    except (OSError, AttributeError):
        # Preserve the existing Qt path if native APIs are unavailable.
        return None
    QApplication.instance().installNativeEventFilter(result)
    return result
