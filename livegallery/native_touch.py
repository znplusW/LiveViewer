"""Touch-popup ownership and Windows gesture policy for the image rectangle.

Qt continues to receive raw touch points for swipe/pinch. Only Windows' own
press-and-hold/right-click recognizer is disabled for contacts on the image;
letterboxing and the rest of the application retain normal system gestures.
"""

from __future__ import annotations

import ctypes
import sys
import weakref

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QEventPoint
from PySide6.QtWidgets import QApplication


WM_TABLET_QUERYSYSTEMGESTURESTATUS = 0x02CC
WM_GESTURENOTIFY = 0x011A
WM_POINTERDOWN = 0x0246
TABLET_DISABLE_PRESSANDHOLD = 0x00000001
GC_ALLGESTURES = 0x00000001


class TouchContextMenuGuard(QObject):
    """Keep the opening long press from becoming a selection in its popup.

    Qt deliberately converts touch updates/releases to mouse events while a
    popup is open. Its menu can then treat the original held contact as a new
    mouse drag and activate an item on release. Observe the raw contact through
    the nested menu event loop, but leave its delivery/cleanup to Qt. Suppress
    only the promoted mouse tail; a new touch or physical mouse press restores
    normal menu interaction immediately.
    """

    def __init__(self, device, contact_ids) -> None:
        super().__init__()
        self._device = device
        self._contacts = set(contact_ids)
        self._armed = bool(self._contacts)
        self._application = QApplication.instance()

    def __enter__(self):
        self._application.installEventFilter(self)
        return self

    def __exit__(self, *exception):
        self._application.removeEventFilter(self)

    def eventFilter(self, receiver, event):
        if not self._armed:
            return False
        event_type = event.type()
        if event_type in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                          QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            if event.pointingDevice() != self._device:
                if event_type == QEvent.Type.TouchBegin:
                    self._armed = False  # An independent touch device takes over.
                return False
            if event_type == QEvent.Type.TouchBegin and not self._contacts:
                self._armed = False  # A deliberate second tap may choose an item.
            elif event_type == QEvent.Type.TouchCancel:
                self._contacts.clear()
            else:
                for point in event.points():
                    if point.state() == QEventPoint.State.Released:
                        self._contacts.discard(point.id())
                    else:
                        self._contacts.add(point.id())
            # Do not consume raw touch: Qt must retire its own active points.
            return False
        if event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                          QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove):
            if event.source() == Qt.MouseEventSource.MouseEventNotSynthesized:
                if event_type == QEvent.Type.MouseButtonPress:
                    self._armed = False
                return False
            if QApplication.activePopupWidget() is not None:
                event.accept()
                return True
        return False


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _PointShort(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int16), ("y", ctypes.c_int16)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32), ("pt", _Point), ("lPrivate", ctypes.c_uint32),
    ]


class _GestureNotify(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("hwndTarget", ctypes.c_void_p), ("ptsLocation", _PointShort),
        ("dwInstanceID", ctypes.c_uint32),
    ]


class _GestureConfig(ctypes.Structure):
    _fields_ = [
        ("dwID", ctypes.c_uint32), ("dwWant", ctypes.c_uint32),
        ("dwBlock", ctypes.c_uint32),
    ]


def signed_screen_point(lparam: int) -> tuple[int, int]:
    """GET_X/Y_LPARAM semantics, including monitors left/above the primary."""
    return ctypes.c_int16(lparam & 0xFFFF).value, ctypes.c_int16((lparam >> 16) & 0xFFFF).value


def native_client_to_viewport(canvas, x: int, y: int) -> QPointF:
    """Convert physical client pixels to Qt's logical viewport coordinates."""
    window = canvas.window()
    ratio = window.devicePixelRatioF()
    position = canvas.viewport().mapFrom(
        window, QPoint(round(x / ratio), round(y / ratio))
    )
    return QPointF(position)


class ImageTouchGestureFilter(QAbstractNativeEventFilter):
    def __init__(self, canvas, user32=None) -> None:
        super().__init__()
        self._canvas = weakref.ref(canvas)
        self._user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._user32.GetAncestor.restype = ctypes.c_void_p
        self._user32.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Point)]
        self._user32.ScreenToClient.restype = ctypes.c_int
        self._user32.SetGestureConfig.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(_GestureConfig), ctypes.c_uint32,
        ]
        self._user32.SetGestureConfig.restype = ctypes.c_int

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) != b"windows_generic_MSG":
            return False, 0
        msg = _Message.from_address(int(message))
        if msg.message not in (WM_TABLET_QUERYSYSTEMGESTURESTATUS, WM_GESTURENOTIFY, WM_POINTERDOWN):
            return False, 0
        canvas = self._canvas()
        if canvas is None or not canvas.isVisible():
            return False, 0
        window = canvas.window()
        window_id = int(window.winId())
        if self._user32.GetAncestor(msg.hwnd, 2) != window_id:  # GA_ROOT
            return False, 0
        if msg.message == WM_GESTURENOTIFY:
            notice = _GestureNotify.from_address(msg.lParam)
            x, y = notice.ptsLocation.x, notice.ptsLocation.y
        else:
            x, y = signed_screen_point(msg.lParam)
        point = _Point(x, y)
        if not self._user32.ScreenToClient(window_id, ctypes.byref(point)):
            return False, 0
        blocked = canvas.image_contains_viewport_point(
            native_client_to_viewport(canvas, point.x, point.y)
        )
        # SetGestureConfig complements the tablet message on Windows 7+.
        # Re-evaluate at each contact so blank areas never inherit image policy.
        config = _GestureConfig(0, 0 if blocked else GC_ALLGESTURES, GC_ALLGESTURES if blocked else 0)
        self._user32.SetGestureConfig(
            msg.hwnd, 0, 1, ctypes.byref(config), ctypes.sizeof(config)
        )
        if msg.message == WM_TABLET_QUERYSYSTEMGESTURESTATUS and blocked:
            return True, TABLET_DISABLE_PRESSANDHOLD
        # WM_GESTURENOTIFY must continue to DefWindowProc (Microsoft docs).
        return False, 0


def install_image_touch_filter(canvas):
    if sys.platform != "win32":
        return None
    native_filter = ImageTouchGestureFilter(canvas)
    application = QApplication.instance()
    application.installNativeEventFilter(native_filter)
    # Explicit removal avoids dereferencing a deleted QWidget while its Python
    # wrapper is still alive during deferred deletion or application shutdown.
    canvas.destroyed.connect(lambda: application.removeNativeEventFilter(native_filter))
    return native_filter
