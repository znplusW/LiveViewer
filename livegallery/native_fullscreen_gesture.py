"""Best-effort, per-HWND fullscreen edge UI policy; no global settings.

Microsoft documents this window property for desktop fullscreen applications.
Readback verifies the property store, not Windows 11 hardware gesture delivery.
https://learn.microsoft.com/windows/win32/properties/props-system-edgegesture-disabletouchwhenfullscreen
https://learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-shgetpropertystoreforwindow
"""
from __future__ import annotations

import ctypes
import os
import sys
import weakref
from contextlib import contextmanager
from itertools import count

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from .system_open import GUID, _ComPtr, _HRESULT, _hresult


VT_EMPTY, VT_BOOL, VARIANT_TRUE = 0, 11, -1
COOKIE_NAME = 'LiveViewer.FullscreenEdgeGesture.Owner'
PROPERTY_GUID = '32CE38B2-2C9A-41B1-9BC5-B3784394AA44'
IID_IPROPERTYSTORE = GUID.parse('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [('fmtid', GUID), ('pid', ctypes.c_uint32)]


class _CountedPointer(ctypes.Structure):
    _fields_ = [('count', ctypes.c_uint32), ('elements', ctypes.c_void_p)]


class _VariantValue(ctypes.Union):
    _fields_ = [('boolVal', ctypes.c_int16), ('pointer', ctypes.c_void_p), ('array', _CountedPointer)]


class PROPVARIANT(ctypes.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('vt', ctypes.c_uint16), ('reserved1', ctypes.c_uint16),
                ('reserved2', ctypes.c_uint16), ('reserved3', ctypes.c_uint16), ('value', _VariantValue)]


EDGE_PROPERTY = PROPERTYKEY(GUID.parse(PROPERTY_GUID), 2)
_cookies = count(1)


@contextmanager
def _property_store(hwnd):
    ole = ctypes.WinDLL('ole32.dll', winmode=0x800)
    shell = ctypes.WinDLL('shell32.dll', winmode=0x800)
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = _HRESULT
    ole.CoUninitialize.argtypes, ole.CoUninitialize.restype = [], None
    ole.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
    ole.PropVariantClear.restype = _HRESULT
    get = shell.SHGetPropertyStoreForWindow
    get.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    get.restype = _HRESULT
    initialized = int(ole.CoInitializeEx(None, 2)) & 0xffffffff
    if initialized != 0x80010106:  # RPC_E_CHANGED_MODE: use the existing apartment.
        _hresult(initialized, 'Cannot initialize window property store')
    pointer, store = ctypes.c_void_p(), None
    try:
        result = get(ctypes.c_void_p(hwnd), ctypes.byref(IID_IPROPERTYSTORE), ctypes.byref(pointer))
        store = _ComPtr(pointer)
        _hresult(result, 'Cannot open window property store')
        if not store.address:
            raise OSError('Window property store returned no interface')
        yield store, ole
    finally:
        if store is not None:
            store.close()
        if initialized in (0, 1):
            ole.CoUninitialize()


class NativeWindowGestureStore:
    """GUI-thread native calls. Every call verifies this process owns the HWND."""

    def __init__(self):
        self.user = ctypes.WinDLL('user32.dll', use_last_error=True, winmode=0x800)
        for name, arguments, result in (
            ('IsWindow', [ctypes.c_void_p], ctypes.c_int),
            ('GetWindowThreadProcessId', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32),
            ('SetPropW', [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p], ctypes.c_int),
            ('GetPropW', [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            ('RemovePropW', [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p)):
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, result

    def is_own_window(self, hwnd):
        pid = ctypes.c_uint32()
        return bool(hwnd and self.user.IsWindow(hwnd)
                    and self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) and pid.value == os.getpid())

    def claim(self, hwnd, cookie):
        if not self.is_own_window(hwnd):
            raise OSError('Native window is no longer owned by LiveViewer')
        if not self.user.SetPropW(hwnd, COOKIE_NAME, ctypes.c_void_p(cookie)):
            raise OSError('Cannot mark fullscreen window identity')

    def matches(self, hwnd, cookie):
        return self.is_own_window(hwnd) and self.user.GetPropW(hwnd, COOKIE_NAME) == cookie

    def release(self, hwnd, cookie):
        if self.matches(hwnd, cookie):
            self.user.RemovePropW(hwnd, COOKIE_NAME)
            if self.matches(hwnd, cookie):
                raise OSError('Cannot remove fullscreen window identity marker')

    @staticmethod
    def _read(store, ole):
        value = PROPVARIANT()
        try:
            _hresult(store.call(5, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
                                 ctypes.byref(EDGE_PROPERTY), ctypes.byref(value)), 'Cannot read fullscreen edge policy')
            return {'vt': value.vt, 'value': bool(value.boolVal) if value.vt == VT_BOOL else None}
        finally:
            ole.PropVariantClear(ctypes.byref(value))

    def read(self, hwnd):
        if not self.is_own_window(hwnd):
            raise OSError('Native window is no longer owned by LiveViewer')
        with _property_store(hwnd) as (store, ole):
            return self._read(store, ole)

    def write(self, hwnd, enabled):
        if not self.is_own_window(hwnd):
            raise OSError('Native window is no longer owned by LiveViewer')
        value = PROPVARIANT()
        value.vt = VT_EMPTY if enabled is None else VT_BOOL
        value.boolVal = VARIANT_TRUE if enabled else 0
        with _property_store(hwnd) as (store, ole):
            _hresult(store.call(6, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
                                 ctypes.byref(EDGE_PROPERTY), ctypes.byref(value)), 'Cannot set fullscreen edge policy')
            # SHGetPropertyStoreForWindow stores SetValue immediately; no Commit.
            actual = self._read(store, ole)
        expected = {'vt': VT_EMPTY, 'value': None} if enabled is None else {'vt': VT_BOOL, 'value': bool(enabled)}
        if actual != expected:
            raise OSError(f'Fullscreen edge property readback mismatch: {actual}')
        return actual


class WindowEdgeGesturePolicy(QObject):
    """Own only tracked window properties, with bounded lifecycle diagnostics."""

    def __init__(self, windows_provider, *, parent=None, backend=None, supported=None):
        super().__init__(parent)
        app = QApplication.instance()
        self.supported = (sys.platform == 'win32' and app.platformName() == 'windows') if supported is None else supported
        self._actual_native = self.supported and backend is None
        self._backend = backend
        self._provider = windows_provider
        self._enabled = False
        self._closed = False
        self._tracked = {}
        self._diagnostics = []
        self._failures = []
        self._sets = self._clears = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.sync_now)

    def _record(self, action, hwnd, **data):
        self._diagnostics.append(dict(action=action, hwnd=hwnd, **data))
        self._diagnostics = self._diagnostics[-32:]

    def _failure(self, action, hwnd, error):
        message = f'{action}: {type(error).__name__}: {error}'
        self._failures.append(message)
        self._failures = self._failures[-16:]
        self._record(action, hwnd, ok=False, error=message)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not self._enabled:
            self._timer.stop()
            self.sync_now()  # Do not retain a system-edge reservation for an explicit [].
        else:
            self.schedule()

    def schedule(self):
        if self.supported and not self._closed and not self._timer.isActive():
            self._timer.start(0)

    def _forget(self, key):
        # QObject destruction may arrive after the provider forgot the window.
        self._tracked.pop(key, None)

    def _clear(self, key):
        record = self._tracked.get(key)
        if record is None:
            return
        hwnd, cookie = record['hwnd'], record['cookie']
        try:
            if self._backend.matches(hwnd, cookie):
                self._backend.write(hwnd, None)
                self._clears += 1
                self._backend.release(hwnd, cookie)
                self._record('clear', hwnd, ok=True, vt=VT_EMPTY)
            else:
                self._record('stale_handle_skipped', hwnd, ok=True)
            window = record['window']()
            if window is not None and isValid(window):
                window.destroyed.disconnect(record['destroyed'])
            self._tracked.pop(key, None)
        except (OSError, ValueError, RuntimeError, AttributeError) as error:
            self._failure('clear', hwnd, error)  # Keep ownership for a later retry.

    @Slot()
    def sync_now(self):
        if not self.supported or self._closed or not isValid(self):
            return
        try:
            if self._backend is None:
                self._backend = NativeWindowGestureStore()
            windows = [window for window in self._provider() if window is not None and isValid(window)]
            wanted = {id(window): window for window in windows
                      if self._enabled and window.isVisible() and window.isFullScreen()
                      and window.testAttribute(Qt.WidgetAttribute.WA_WState_Created)}
            for key, record in tuple(self._tracked.items()):
                window = wanted.get(key)
                if window is None or int(window.winId()) != record['hwnd']:
                    self._clear(key)
            for key, window in wanted.items():
                record = self._tracked.get(key)
                if record is not None and record.get('set'):
                    continue
                hwnd = int(window.winId())
                if record is None:
                    cookie = (os.getpid() << 20) + next(_cookies)
                    self._backend.claim(hwnd, cookie)
                    owner = weakref.ref(self)
                    def destroyed(_object=None, key=key, owner=owner):
                        policy = owner()
                        if policy is not None:
                            policy._forget(key)
                    record = self._tracked[key] = dict(window=weakref.ref(window), hwnd=hwnd,
                                                      cookie=cookie, set=False, destroyed=destroyed)
                    window.destroyed.connect(destroyed)
                if not self._backend.matches(hwnd, record['cookie']):
                    self._record('stale_handle_skipped', hwnd, ok=True)
                    continue
                self._backend.write(hwnd, True)
                record['set'] = True
                self._sets += 1
                self._record('set', hwnd, ok=True, vt=VT_BOOL, value=True)
        except (OSError, ValueError, RuntimeError, AttributeError) as error:
            self._failure('sync', 0, error)

    def observe(self, receiver, event):
        if not self.supported or self._closed:
            return
        kind = event.type()
        if kind not in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.DeferredDelete,
                        QEvent.Type.WindowStateChange, QEvent.Type.WinIdChange):
            return
        if not isinstance(receiver, QWidget) or not receiver.isWindow():
            return
        key = id(receiver)
        if kind in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.DeferredDelete, QEvent.Type.WinIdChange):
            self._clear(key)  # Tracked windows remain eligible even after provider removal.
        if kind not in (QEvent.Type.Close, QEvent.Type.DeferredDelete):
            self.schedule()

    def report(self):
        return {'supported': bool(self.supported), 'native_api_used': bool(self._actual_native and self._sets),
                'property_guid': PROPERTY_GUID, 'property_id': 2, 'global_policy_changed': False,
                'set_readback_count': self._sets, 'empty_readback_count': self._clears,
                'failures': list(self._failures), 'diagnostics': list(self._diagnostics),
                'physical_touch_delivery_verified': False}

    def close(self):
        if self._closed:
            return
        self._timer.stop()
        for key in tuple(self._tracked):
            self._clear(key)
        self._closed = True
        self._provider = lambda: ()
