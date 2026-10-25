"""Windows input-injection verification, only invoked by explicit self-tests.

Exercises user32 -> native event filter -> Escape at physical pixel 0 / last
pixel. An injected pointer is not evidence of a physical digitizer delivering
bezel input; that remains a separate hardware acceptance check.
"""
import ctypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication, QWidget

from .native_edge_touch import PointerInfo
from .native_touch import _Point
from .fullscreen_edge_gesture import FullscreenEdgeGesture


class Rect(ctypes.Structure):
    _fields_ = [('left', ctypes.c_int32), ('top', ctypes.c_int32),
                ('right', ctypes.c_int32), ('bottom', ctypes.c_int32)]


class TouchInfo(ctypes.Structure):
    _fields_ = [('pointerInfo', PointerInfo), ('touchFlags', ctypes.c_uint32),
                ('touchMask', ctypes.c_uint32), ('rcContact', Rect),
                ('rcContactRaw', Rect), ('orientation', ctypes.c_uint32),
                ('pressure', ctypes.c_uint32)]


def check_native_bezel_input():
    if sys.platform != 'win32' or QApplication.instance().platformName() != 'windows':
        raise RuntimeError('Native bezel input probe requires qwindows')
    user = ctypes.WinDLL('user32.dll', use_last_error=True, winmode=0x800)
    for name, args, result in (
        ('InitializeTouchInjection', [ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        ('InjectTouchInput', [ctypes.c_uint32, ctypes.POINTER(TouchInfo)], ctypes.c_int),
        ('ClientToScreen', [ctypes.c_void_p, ctypes.POINTER(_Point)], ctypes.c_int),
        ('GetClientRect', [ctypes.c_void_p, ctypes.POINTER(Rect)], ctypes.c_int),
        ('SetForegroundWindow', [ctypes.c_void_p], ctypes.c_int),
        ('GetForegroundWindow', [], ctypes.c_void_p)):
        method = getattr(user, name)
        method.argtypes, method.restype = args, result
    if not user.InitializeTouchInjection(2, 3):  # TOUCH_FEEDBACK_NONE
        raise ctypes.WinError(ctypes.get_last_error())
    previous = QApplication.activeWindow()
    target = QWidget()
    target.setWindowTitle('LiveViewer native edge input self-test')
    target.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
    gesture = FullscreenEdgeGesture(lambda: (target,), enabled_edges=('left', 'right', 'top'))
    gesture.escapeRequested.connect(lambda _: target.showNormal())
    escape = QSignalSpy(gesture.escapeRequested)
    cases = []
    contact = TouchInfo()
    contact.pointerInfo.pointerType = 2
    contact.pointerInfo.pointerId = 8
    contact.touchMask = 7
    contact.orientation = 90
    contact.pressure = 32000

    def inject(x, y, flags):
        contact.pointerInfo.ptPixelLocation = _Point(x, y)
        contact.pointerInfo.pointerFlags = flags
        contact.rcContact = Rect(x - 2, y - 2, x + 2, y + 2)
        QTest.qWait(12)
        if not user.InjectTouchInput(1, ctypes.byref(contact)):
            raise ctypes.WinError(ctypes.get_last_error())
        QTest.qWait(30)

    try:
        for edge in ('left', 'right', 'top'):
            target.showFullScreen()
            target.raise_()
            target.activateWindow()
            QTest.qWait(100)
            hwnd = int(target.winId())
            user.SetForegroundWindow(hwnd)
            QTest.qWait(60)
            gesture.native_policy.sync_now()
            assert user.GetForegroundWindow() == hwnd, 'Probe window did not acquire foreground'
            origin, area = _Point(), Rect()
            assert user.ClientToScreen(hwnd, ctypes.byref(origin))
            assert user.GetClientRect(hwnd, ctypes.byref(area))
            x = origin.x + (0 if edge == 'left' else area.right - 1 if edge == 'right' else area.right // 2)
            y = origin.y + (0 if edge == 'top' else area.bottom // 2)
            length = round(130 * target.devicePixelRatioF())
            dx, dy = ((length, 0) if edge == 'left' else (-length, 0) if edge == 'right' else (0, length))
            before = escape.count()
            claims = gesture.native_touch.claims
            try:
                inject(x, y, 0x10000 | 0x2 | 0x4)
                for step in range(1, 6):
                    inject(x + dx * step // 5, y + dy * step // 5, 0x20000 | 0x2 | 0x4)
            finally:
                # UP must use the last UPDATE coordinates. On an assertion
                # failure above the final release is still attempted.
                last = contact.pointerInfo.ptPixelLocation
                inject(last.x, last.y, 0x40000)
            assert escape.count() == before + 1, f'{edge}: native edge did not emit one Escape'
            assert not target.isFullScreen(), f'{edge}: did not exit fullscreen'
            assert gesture.native_touch.claims == claims + 1, f'{edge}: raw pointer route was not exercised'
            assert not gesture.native_touch.ids, f'{edge}: contact ownership leaked'
            cases.append({'edge': edge, 'start_physical': [x, y], 'native_route': True, 'escaped_once': True})
        return {'injected_physical_boundary_cases': cases, 'raw_pointer_api_used': True,
                'physical_digitizer_verified': False, 'global_policy_changed': False}
    finally:
        gesture.close()
        target.close()
        target.deleteLater()
        if previous is not None:
            previous.activateWindow()
        QTest.qWait(30)
