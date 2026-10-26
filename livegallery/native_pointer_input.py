"""Windows press provenance and physical touchpad-button hold authorization.

A short, reference-counted touchpad-capable thread scope identifies the current
native mouse message; no message dispatch occurs inside that scope. Only PTP
Button 1 from a fresh successful HID report can authorize a touchpad hold.
Ordinary mouse presses remain independent of concurrent touchpad contacts.

https://learn.microsoft.com/windows/win32/input-precisiontouchpad/registertouchpadcapable
https://learn.microsoft.com/windows-hardware/design/component-guidelines/touchpad-buttons-report-level-usages
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import struct
import time
import sys
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject
from PySide6.QtWidgets import QApplication

from .native_touch import _Message

WM_INPUT = 0x00FF
RIM_INPUT, RIM_TYPEHID = 0, 2
RID_INPUT, RID_HEADER = 0x10000003, 0x10000005
RIDI_PREPARSEDDATA, RIDI_DEVICEINFO = 0x20000005, 0x2000000B
ERROR_INSUFFICIENT_BUFFER = 122
UINT_ERROR = 0xFFFFFFFF
MAX_PACKET, MAX_REPORTS, MAX_CAPS, MAX_DEVICES = 1 << 20, 64, 512, 32
HIDP_SUCCESS = 0x00110000
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203


@dataclass(frozen=True)
class SourceEvidence:
    device_type: int | None = None
    origin_id: int | None = None
    scope_entered: bool = False
    scope_restored: bool = True
    query_succeeded: bool = False
    reason: str = 'unavailable'


def scoped_source(register, query):
    """No dispatch, event pump, diagnostic callback, or Qt call in this scope."""
    try:
        entered = bool(register(True))
    except Exception:
        return SourceEvidence(reason='scope_enter_failed')
    if not entered:
        return SourceEvidence(reason='scope_enter_failed')
    source, restored = None, False
    try:
        source = query()
    except Exception:
        pass
    finally:
        try:
            restored = bool(register(False))
        except Exception:
            pass
    if not restored:
        return SourceEvidence(scope_entered=True, scope_restored=False, reason='scope_restore_failed')
    if source is None:
        return SourceEvidence(scope_entered=True, reason='source_query_failed')
    return SourceEvidence(int(source[0]), int(source[1]), True, True, True, 'ok')


@dataclass(frozen=True)
class PressAuthorization:
    allow_hold: bool | None
    source: str
    reason: str
    epoch: int
    timestamp: int
    hwnd: int
    sequence: int
    button1: bool | None = None
    ptp_device: int | None = None


class PressPermissions:
    """Pure state machine. Unknown provenance is never a physical-click grant."""
    def __init__(self, clock=time.monotonic, freshness=.25, capacity=128):
        self.clock, self.freshness, self.capacity = clock, freshness, capacity
        self.epoch = self.sequence = 0
        self.active = None
        self.records = OrderedDict()
        self.ptp = {}

    def update_ptp(self, device, button):
        if button is not None and type(button) is not bool:
            raise ValueError('button_state_must_be_bool_or_unknown')
        if device not in self.ptp and len(self.ptp) >= MAX_DEVICES:
            self.ptp.clear()
            self.active = None
            return
        now = self.clock()
        token = self.active
        if token is not None and token.source == 'touchpad' and token.allow_hold is True:
            previous = self.ptp.get(token.ptp_device)
            # A release, parse failure, report gap, or another reporting pad
            # ends this grant. Later button-down reports cannot revive it.
            if (device != token.ptp_device or button is not True or not previous
                    or not 0 <= now - previous[1] <= self.freshness):
                self.active = None
        self.ptp[device] = (button, now, self.epoch)

    def invalidate_ptp(self):
        self.ptp.clear()
        if self.active is not None and self.active.source == 'touchpad':
            self.active = None

    def cancel(self):
        self.epoch += 1
        self.active = None
        self.ptp.clear()
        # Keep bounded exact records: late Qt delivery must get an invalid
        # token rather than falling back as if no native press ever existed.

    def release(self):
        self.active = None

    def press(self, hwnd, timestamp, source):
        key = int(hwnd), int(timestamp) & UINT_ERROR, 1
        self.sequence += 1
        allowed, label, reason, button, device = None, 'unknown', source.reason, None, None
        if source.query_succeeded and source.scope_entered and source.scope_restored:
            if source.origin_id != 1:
                reason = 'source_not_hardware_or_uiaccess'
            elif source.device_type == 2:
                allowed, label, reason = True, 'mouse', 'scoped_mouse_source'
            elif source.device_type == 4:
                label, reason = 'touch', 'touch_uses_its_own_contact_path'
            elif source.device_type == 16:
                label, reason = 'touchpad', 'missing_or_stale_button_report'
                now = self.clock()
                fresh = [(d, state[0]) for d, state in self.ptp.items()
                         if state[2] == self.epoch and 0 <= now - state[1] <= self.freshness]
                if len(fresh) > 1:
                    reason = 'ambiguous_touchpad_devices'
                elif len(fresh) == 1:
                    device, button = fresh[0]
                    allowed = button
                    reason = ('mechanical_button_down' if button is True else
                              'mechanical_button_up' if button is False else 'button_report_unknown')
            else:
                reason = 'source_device_unknown'
        if key in self.records:
            allowed, reason = None, 'native_timestamp_collision'
        token = PressAuthorization(allowed, label, reason, self.epoch, key[1], key[0],
                                   self.sequence, button, device)
        self.records[key] = token
        self.records.move_to_end(key)
        while len(self.records) > self.capacity:
            self.records.popitem(last=False)
        self.active = token
        return token

    def permission_for(self, hwnd, timestamp, button=1):
        number = getattr(button, 'value', button)
        return self.records.get((int(hwnd), int(timestamp) & UINT_ERROR, int(number)))

    def is_current(self, token):
        if not isinstance(token, PressAuthorization) or token is not self.active or token.epoch != self.epoch:
            return False
        if token.source == 'touchpad' and token.allow_hold is True:
            state = self.ptp.get(token.ptp_device)
            return bool(state and state[0] is True and state[2] == self.epoch
                        and 0 <= self.clock() - state[1] <= self.freshness)
        return True


class INPUT_MESSAGE_SOURCE(C.Structure):
    _fields_ = [('device', W.DWORD), ('origin', W.DWORD)]


class RAWINPUTDEVICE(C.Structure):
    _fields_ = [('page', W.USHORT), ('usage', W.USHORT), ('flags', W.DWORD), ('target', W.HWND)]


class RAWINPUTHEADER(C.Structure):
    _fields_ = [('kind', W.DWORD), ('size', W.DWORD), ('device', W.HANDLE), ('wparam', W.WPARAM)]


class HIDINFO(C.Structure):
    _fields_ = [('vendor', W.DWORD), ('product', W.DWORD), ('version', W.DWORD),
                ('page', W.USHORT), ('usage', W.USHORT)]


class DEVICEUNION(C.Union):
    _fields_ = [('hid', HIDINFO), ('padding', W.DWORD * 6)]


class DEVICEINFO(C.Structure):
    _fields_ = [('size', W.DWORD), ('kind', W.DWORD), ('data', DEVICEUNION)]


class HIDP_CAPS(C.Structure):
    _fields_ = [(name, W.USHORT) for name in ('Usage', 'UsagePage', 'InputReportByteLength',
        'OutputReportByteLength', 'FeatureReportByteLength')] + [('Reserved', W.USHORT * 17)] + [
        (name, W.USHORT) for name in ('NumberLinkCollectionNodes', 'NumberInputButtonCaps',
        'NumberInputValueCaps', 'NumberInputDataIndices', 'NumberOutputButtonCaps',
        'NumberOutputValueCaps', 'NumberOutputDataIndices', 'NumberFeatureButtonCaps',
        'NumberFeatureValueCaps', 'NumberFeatureDataIndices')]


class HIDP_BUTTON_CAPS(C.Structure):
    _fields_ = [('UsagePage', W.USHORT), ('ReportID', W.BYTE), ('IsAlias', W.BYTE),
        ('BitField', W.USHORT), ('LinkCollection', W.USHORT), ('LinkUsage', W.USHORT),
        ('LinkUsagePage', W.USHORT), ('IsRange', W.BYTE), ('IsStringRange', W.BYTE),
        ('IsDesignatorRange', W.BYTE), ('IsAbsolute', W.BYTE), ('ReportCount', W.USHORT),
        ('Reserved2', W.USHORT), ('Reserved', W.DWORD * 9), ('Usages', W.USHORT * 8)]


class HIDP_VALUE_CAPS(C.Structure):
    _fields_ = HIDP_BUTTON_CAPS._fields_[:11] + [('HasNull', W.BYTE), ('Reserved', W.BYTE),
        ('BitSize', W.USHORT), ('ReportCount', W.USHORT), ('Reserved2', W.USHORT * 5),
        ('UnitsExp', W.DWORD), ('Units', W.DWORD), ('LogicalMin', W.LONG),
        ('LogicalMax', W.LONG), ('PhysicalMin', W.LONG), ('PhysicalMax', W.LONG),
        ('Usages', W.USHORT * 8)]


def abi_sizes():
    return {name: C.sizeof(value) for name, value in globals().items()
            if name in ('RAWINPUTDEVICE', 'RAWINPUTHEADER', 'DEVICEINFO',
                        'HIDP_CAPS', 'HIDP_BUTTON_CAPS', 'HIDP_VALUE_CAPS')}


def split_hid_packet(packet: bytes, pointer_bytes: int = C.sizeof(C.c_void_p)):
    """Bounded data-only validation; returns ephemeral reports, never log this."""
    if pointer_bytes not in (4, 8):
        raise ValueError('unsupported_pointer_size')
    header_size = 8 + 2 * pointer_bytes
    if not header_size + 8 <= len(packet) <= MAX_PACKET:
        raise ValueError('raw_packet_size')
    kind, size = struct.unpack_from('<II', packet)
    if kind != RIM_TYPEHID or size != len(packet):
        raise ValueError('raw_header_mismatch')
    device = int.from_bytes(packet[8:8 + pointer_bytes], 'little')
    wparam = int.from_bytes(packet[8 + pointer_bytes:header_size], 'little')
    if (wparam & 0xFF) != RIM_INPUT:
        raise ValueError('background_packet')
    report_bytes, count = struct.unpack_from('<II', packet, header_size)
    if not 1 <= count <= MAX_REPORTS or not 1 <= report_bytes <= 65535:
        raise ValueError('raw_report_dimensions')
    start, end = header_size + 8, header_size + 8 + report_bytes * count
    if end > size:
        raise ValueError('raw_report_overrun')
    return device, [packet[start + i * report_bytes:start + (i + 1) * report_bytes]
                    for i in range(count)]


def _includes(cap, usage):
    return (cap.Usages[0] <= usage <= cap.Usages[1]) if cap.IsRange else cap.Usages[0] == usage


class _WinApi:
    """Read-only queries except explicit register/remove; GUI-thread use only."""
    def __init__(self):
        if not hasattr(C, 'WinDLL'):
            raise OSError('Windows native required')
        expected = {'RAWINPUTDEVICE': 16, 'RAWINPUTHEADER': 24, 'DEVICEINFO': 32,
                    'HIDP_CAPS': 64, 'HIDP_BUTTON_CAPS': 72, 'HIDP_VALUE_CAPS': 72}
        if C.sizeof(C.c_void_p) != 8 or abi_sizes() != expected:
            raise RuntimeError('unsupported_native_ABI')
        self.user = C.WinDLL('user32', use_last_error=True)
        self.hid = C.WinDLL('hid', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.cache = {}
        def bind(dll, name, restype, *args):
            f = getattr(dll, name); f.restype, f.argtypes = restype, list(args)
        bind(self.user, 'GetRegisteredRawInputDevices', W.UINT, C.POINTER(RAWINPUTDEVICE), C.POINTER(W.UINT), W.UINT)
        bind(self.user, 'RegisterRawInputDevices', W.BOOL, C.POINTER(RAWINPUTDEVICE), W.UINT, W.UINT)
        bind(self.user, 'GetRawInputData', W.UINT, W.HANDLE, W.UINT, C.c_void_p, C.POINTER(W.UINT), W.UINT)
        bind(self.user, 'GetRawInputDeviceInfoW', W.UINT, W.HANDLE, W.UINT, C.c_void_p, C.POINTER(W.UINT))
        bind(self.user, 'GetForegroundWindow', W.HWND)
        bind(self.user, 'GetWindowThreadProcessId', W.DWORD, W.HWND, C.POINTER(W.DWORD))
        bind(self.user, 'GetAncestor', W.HWND, W.HWND, W.UINT)
        bind(self.kernel, 'GetCurrentProcessId', W.DWORD)
        bind(self.hid, 'HidP_GetCaps', W.LONG, C.c_void_p, C.POINTER(HIDP_CAPS))
        bind(self.hid, 'HidP_GetButtonCaps', W.LONG, C.c_int, C.POINTER(HIDP_BUTTON_CAPS), C.POINTER(W.USHORT), C.c_void_p)
        bind(self.hid, 'HidP_GetValueCaps', W.LONG, C.c_int, C.POINTER(HIDP_VALUE_CAPS), C.POINTER(W.USHORT), C.c_void_p)
        bind(self.hid, 'HidP_MaxUsageListLength', W.ULONG, C.c_int, W.USHORT, C.c_void_p)
        bind(self.hid, 'HidP_GetUsages', W.LONG, C.c_int, W.USHORT, W.USHORT, C.POINTER(W.USHORT),
             C.POINTER(W.ULONG), C.c_void_p, C.c_void_p, W.ULONG)
        bind(self.hid, 'HidP_GetUsageValue', W.LONG, C.c_int, W.USHORT, W.USHORT, W.USHORT,
             C.POINTER(W.ULONG), C.c_void_p, C.c_void_p, W.ULONG)
        self.capable = getattr(self.user, 'RegisterTouchpadCapableThread', None)
        self.source_query = getattr(self.user, 'GetCurrentInputMessageSource', None)
        if self.capable is not None:
            self.capable.argtypes, self.capable.restype = [W.BOOL], W.BOOL
        if self.source_query is not None:
            self.source_query.argtypes = [C.POINTER(INPUT_MESSAGE_SOURCE)]
            self.source_query.restype = W.BOOL

    def current_source(self):
        if self.capable is None or self.source_query is None:
            return SourceEvidence(reason='source_api_unavailable')
        def query():
            value = INPUT_MESSAGE_SOURCE()
            return (value.device, value.origin) if self.source_query(C.byref(value)) else None
        return scoped_source(self.capable, query)

    def restore_source_scope(self):
        return bool(self.capable is not None and self.capable(False))

    def root_window(self, hwnd):
        return int(self.user.GetAncestor(hwnd, 2) or hwnd or 0)

    def own_window(self, hwnd):
        pid = W.DWORD()
        return bool(hwnd and self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
                    and pid.value == self.kernel.GetCurrentProcessId())

    def foreground_is_ours(self):
        return self.own_window(self.user.GetForegroundWindow())

    def registrations(self):
        count = W.UINT()
        C.set_last_error(0)
        result = self.user.GetRegisteredRawInputDevices(None, C.byref(count), C.sizeof(RAWINPUTDEVICE))
        if result == UINT_ERROR and C.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            raise OSError('registration_query_failed')
        if count.value > MAX_CAPS:
            raise ValueError('registration_count_bound')
        if not count.value:
            return []
        buffer = (RAWINPUTDEVICE * count.value)()
        capacity = count.value
        result = self.user.GetRegisteredRawInputDevices(buffer, C.byref(count), C.sizeof(RAWINPUTDEVICE))
        if result == UINT_ERROR or result > capacity:
            raise OSError('registration_read_failed')
        return [(b.page, b.usage, b.flags, int(b.target or 0)) for b in buffer[:result]]

    def register(self, hwnd=0):
        item = RAWINPUTDEVICE(13, 5, 0, hwnd)
        if not self.user.RegisterRawInputDevices(C.byref(item), 1, C.sizeof(item)):
            raise OSError('foreground_registration_failed')

    def remove(self):
        item = RAWINPUTDEVICE(13, 5, 1, None)  # RIDEV_REMOVE requires NULL hwnd.
        if not self.user.RegisterRawInputDevices(C.byref(item), 1, C.sizeof(item)):
            raise OSError('foreground_unregister_failed')

    def header(self, handle):
        header, size = RAWINPUTHEADER(), W.UINT(C.sizeof(RAWINPUTHEADER))
        result = self.user.GetRawInputData(handle, RID_HEADER, C.byref(header), C.byref(size), C.sizeof(header))
        if result != C.sizeof(header) or size.value != C.sizeof(header):
            raise OSError('raw_header_read_failed')
        if not C.sizeof(header) + 8 <= header.size <= MAX_PACKET:
            raise ValueError('raw_header_size_bound')
        return {'kind': header.kind, 'size': header.size, 'device': int(header.device or 0),
                'wparam': int(header.wparam)}

    def packet(self, handle, expected_size):
        size = W.UINT()
        result = self.user.GetRawInputData(handle, RID_INPUT, None, C.byref(size), C.sizeof(RAWINPUTHEADER))
        if result != 0 or size.value != expected_size or not 32 <= size.value <= MAX_PACKET:
            raise ValueError('raw_packet_query_bound')
        buffer, capacity = C.create_string_buffer(size.value), size.value
        result = self.user.GetRawInputData(handle, RID_INPUT, buffer, C.byref(size), C.sizeof(RAWINPUTHEADER))
        if result != capacity or size.value != capacity:
            raise OSError('raw_packet_read_failed')
        return buffer.raw[:capacity]

    def descriptor(self, device):
        if device in self.cache:
            return self.cache[device]
        if len(self.cache) >= MAX_DEVICES:
            raise ValueError('descriptor_cache_bound')
        info = DEVICEINFO(); info.size = C.sizeof(info); size = W.UINT(info.size)
        result = self.user.GetRawInputDeviceInfoW(device, RIDI_DEVICEINFO, C.byref(info), C.byref(size))
        if result == UINT_ERROR:
            raise OSError('device_info_failed')
        if info.kind != RIM_TYPEHID or (info.data.hid.page, info.data.hid.usage) != (13, 5):
            self.cache[device] = None
            return None
        size = W.UINT()
        result = self.user.GetRawInputDeviceInfoW(device, RIDI_PREPARSEDDATA, None, C.byref(size))
        if result == UINT_ERROR or not 1 <= size.value <= MAX_PACKET:
            raise ValueError('preparsed_query_bound')
        buffer, capacity = C.create_string_buffer(size.value), size.value
        result = self.user.GetRawInputDeviceInfoW(device, RIDI_PREPARSEDDATA, buffer, C.byref(size))
        if result == UINT_ERROR or result > capacity or size.value > capacity:
            raise OSError('preparsed_read_failed')
        caps = HIDP_CAPS()
        if self.hid.HidP_GetCaps(buffer, C.byref(caps)) != HIDP_SUCCESS:
            raise OSError('hid_caps_failed')
        if (caps.UsagePage, caps.Usage) != (13, 5) or not 1 <= caps.InputReportByteLength <= 65535:
            raise ValueError('hid_caps_invalid')
        def read_caps(kind, count, function):
            if count > MAX_CAPS:
                raise ValueError('hid_caps_count_bound')
            if count == 0:
                return []
            values, length = (kind * count)(), W.USHORT(count)
            if function(0, values, C.byref(length), buffer) != HIDP_SUCCESS or length.value > count:
                raise OSError('hid_caps_list_failed')
            return list(values[:length.value])
        buttons = read_caps(HIDP_BUTTON_CAPS, caps.NumberInputButtonCaps, self.hid.HidP_GetButtonCaps)
        values = read_caps(HIDP_VALUE_CAPS, caps.NumberInputValueCaps, self.hid.HidP_GetValueCaps)
        button_reports = {b.ReportID for b in buttons if b.UsagePage == 9
                          and b.LinkCollection == 0 and _includes(b, 1)}
        contact_reports = {v.ReportID for v in values if v.UsagePage == 13
                           and v.LinkCollection == 0 and _includes(v, 0x54)}
        max_usages = int(self.hid.HidP_MaxUsageListLength(0, 9, buffer))
        if button_reports and not 1 <= max_usages <= MAX_CAPS:
            raise ValueError('hid_usage_list_bound')
        descriptor = {'buffer': buffer, 'report_bytes': caps.InputReportByteLength,
                      'button_reports': button_reports, 'contact_reports': contact_reports,
                      'max_usages': max_usages}
        self.cache[device] = descriptor  # Owns preparsed memory for each parse.
        return descriptor

    def decode(self, descriptor, report):
        if not report or len(report) > descriptor['report_bytes']:
            raise ValueError('hid_report_length_bound')
        report_id, data = report[0], C.create_string_buffer(report, len(report))
        button, contact, button_status, contact_status = None, None, 'not_in_report', 'not_in_report'
        if report_id in descriptor['button_reports']:
            usages = (W.USHORT * descriptor['max_usages'])()
            length = W.ULONG(len(usages))
            status = self.hid.HidP_GetUsages(0, 9, 0, usages, C.byref(length),
                                           descriptor['buffer'], data, len(report))
            if status == HIDP_SUCCESS and length.value <= len(usages):
                button, button_status = 1 in usages[:length.value], 'ok'
            else:
                button_status = f'parse_failed_0x{status & UINT_ERROR:08x}'
        if report_id in descriptor['contact_reports']:
            value = W.ULONG()
            status = self.hid.HidP_GetUsageValue(0, 13, 0, 0x54, C.byref(value),
                                                descriptor['buffer'], data, len(report))
            if status == HIDP_SUCCESS and value.value <= 255:
                contact, contact_status = value.value, 'ok'
            else:
                contact_status = f'parse_failed_0x{status & UINT_ERROR:08x}'
        return {'report_id': report_id, 'button1': button, 'button_status': button_status,
                'contact_count': contact, 'contact_status': contact_status}


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self.owner = weakref.ref(owner)

    def nativeEventFilter(self, event_type, message):
        owner = self.owner()
        if owner is not None and not owner.closed:
            saved_error = C.get_last_error()
            try:
                if bytes(event_type) in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
                    owner.observe_message(_Message.from_address(int(message)))
            except Exception:
                owner._error('native_filter')
                owner.permissions.cancel()
            finally:
                C.set_last_error(saved_error)
        return False, 0  # Qt/DefWindowProc still owns message handling/cleanup.


class NativePointerInput(QObject):
    """One optional observer per QApplication, with no global input settings."""
    _attribute = '_liveviewer_native_pointer_input'

    def __init__(self, application=None, *, backend=None, clock=time.monotonic):
        super().__init__(application)
        self.application = application
        self.backend = backend
        self.permissions = PressPermissions(clock=clock)
        self.native_filter = None
        self.started = self.registered = self.closed = False
        self.counters = dict(errors=0, reports=0, raw_messages=0, native_presses=0,
                             scope_restore_failures=0, parse_unknown=0)
        self.last_error = None
        self.registration_status = 'not_started'
        self.scope_healthy = True
        self.scope_debt = 0
        self.scope_debt_thread = None
        self.scope_cleanup_status = 'not_needed'

    @classmethod
    def ensure_for_application(cls, application=None):
        app = application or QApplication.instance()
        if app is None or sys.platform != 'win32' or app.platformName() != 'windows':
            return None
        current = getattr(app, cls._attribute, None)
        if current is not None and not current.closed:
            return current
        current = cls(app)
        setattr(app, cls._attribute, current)
        current.start()
        return current

    def _error(self, phase):
        self.counters['errors'] += 1
        self.last_error = phase

    @staticmethod
    def _matching(entries):
        return [entry for entry in entries if entry[0] == 13 and entry[1] in (0, 5)]

    def start(self):
        if self.started or self.closed:
            return False
        try:
            if self.backend is None:
                self.backend = _WinApi()
        except Exception:
            self._error('backend_start')
            self.registration_status = 'backend_unavailable'
            return False
        try:
            # NULL target + flags 0 sends foreground input to this process's
            # current focus window, including detached previews; no INPUTSINK.
            # https://learn.microsoft.com/windows/win32/api/winuser/ns-winuser-rawinputdevice
            if self._matching(self.backend.registrations()):
                self.registration_status = 'existing_registration_refused'
            else:
                self.backend.register(0)
                self.registered = True
                self.registration_status = 'foreground_registered'
        except Exception:
            self._error('registration_start')
            self.registration_status = 'registration_failed'
        # Even failed/conflicting HID registration must observe native presses:
        # a real touchpad press becomes an explicit unknown/denied token.
        try:
            if self.application is not None:
                self.native_filter = _NativeFilter(self)
                self.application.installNativeEventFilter(self.native_filter)
                self.application.installEventFilter(self)
                self.application.aboutToQuit.connect(self.close)
            self.started = True
            return self.registered
        except Exception:
            self._error('filter_start')
            # A partial registration must still be released by close().
            return False

    def authorize(self, event, hwnd):
        return self.permission_for(hwnd, event.timestamp(), event.button())

    def permission_for(self, hwnd, timestamp, button=1):
        return self.permissions.permission_for(hwnd, timestamp, button)

    def is_current(self, token):
        return not self.closed and self.permissions.is_current(token)

    def cancel(self):
        self.permissions.cancel()

    def eventFilter(self, receiver, event):
        if event.type() in (QEvent.Type.ApplicationDeactivate, QEvent.Type.DevicePixelRatioChange):
            self.cancel()
        return False

    def observe_message(self, msg):
        if self.closed or self.backend is None:
            return
        root = int(msg.hwnd or 0)
        try:
            kind, hwnd = int(msg.message), int(msg.hwnd or 0)
            if kind not in (WM_INPUT, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK,
                            0x001F, 0x0082):  # CANCELMODE / NCDESTROY
                return
            if not self.backend.own_window(hwnd):
                return
            if kind in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                root = self.backend.root_window(hwnd)
            if not self.backend.foreground_is_ours():
                self.cancel()
                return
            if kind == WM_INPUT:
                if not self.registered or (int(msg.wParam) & 0xFF) != RIM_INPUT:
                    return
                self.counters['raw_messages'] += 1
                header = self.backend.header(msg.lParam)
                if header['kind'] != RIM_TYPEHID or (header['wparam'] & 0xFF) != RIM_INPUT:
                    return
                descriptor = self.backend.descriptor(header['device'])
                if descriptor is None:
                    return
                device, reports = split_hid_packet(self.backend.packet(msg.lParam, header['size']))
                if device != header['device']:
                    raise ValueError('raw_device_mismatch')
                for report in reports:
                    self.counters['reports'] += 1
                    data = self.backend.decode(descriptor, report)
                    button = data['button1'] if data['button_status'] == 'ok' else None
                    if button is None:
                        self.counters['parse_unknown'] += 1
                    self.permissions.update_ptp(device, button)
                return
            if kind == WM_LBUTTONUP:
                self.permissions.release()
            elif kind in (0x001F, 0x0082):
                self.cancel()
            else:
                source = (self.backend.current_source() if self.scope_healthy else
                          SourceEvidence(reason='scope_previously_unbalanced'))
                if not source.scope_restored:
                    self.scope_healthy = False
                    self.counters['scope_restore_failures'] += 1
                    if source.scope_entered:
                        self.scope_debt = 1
                        self.scope_debt_thread = threading.get_ident()
                        self.scope_cleanup_status = 'pending'
                self.counters['native_presses'] += 1
                self.permissions.press(root, int(msg.time), source)
        except Exception:
            self._error('observe')
            self.permissions.invalidate_ptp()
            if int(msg.message) in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                # A known real press remains explicitly unknown, rather than
                # falling into the injection/no-native-record compatibility path.
                self.permissions.press(root, int(msg.time), SourceEvidence(reason='native_query_error'))

    def diagnostic_snapshot(self):
        return {'started': self.started, 'closed': self.closed, 'registered': self.registered,
                'registration_status': self.registration_status, 'scope_healthy': self.scope_healthy,
                'scope_debt': self.scope_debt, 'scope_cleanup_status': self.scope_cleanup_status,
                'counters': dict(self.counters), 'last_error': self.last_error,
                'epoch': self.permissions.epoch, 'ptp_devices': len(self.permissions.ptp),
                'active_permission': None if self.permissions.active is None else {
                    k: getattr(self.permissions.active, k) for k in
                    ('allow_hold', 'source', 'reason', 'epoch', 'timestamp', 'hwnd', 'sequence', 'button1')},
                'background_input': False, 'legacy_input_suppressed': False}

    summary = diagnostic_snapshot

    def close(self):
        if self.closed:
            return
        self.cancel()
        if self.scope_debt:
            if self.scope_debt_thread != threading.get_ident():
                self.scope_cleanup_status = 'wrong_thread_left_untouched'
            else:
                try:
                    if self.backend.restore_source_scope():
                        self.scope_debt = 0
                        self.scope_cleanup_status = 'restored'
                    else:
                        self.scope_cleanup_status = 'restore_failed'
                        self._error('scope_cleanup')
                except Exception:
                    self.scope_cleanup_status = 'restore_failed'
                    self._error('scope_cleanup')
        try:
            if self.registered:
                if self._matching(self.backend.registrations()) == [(13, 5, 0, 0)]:
                    self.backend.remove()
                    self.registered = False
                    self.registration_status = 'unregistered'
                else:
                    self.registered = False
                    self.registration_status = 'registration_changed_left_untouched'
        except Exception:
            self._error('close')
            self.registration_status = 'unregister_failed'
        finally:
            if self.application is not None:
                if self.native_filter is not None:
                    self.application.removeNativeEventFilter(self.native_filter)
                self.application.removeEventFilter(self)
            self.closed = True


def ensure_for_application(application=None):
    """Optional Windows-only QApplication singleton used by ViewerCanvas."""
    return NativePointerInput.ensure_for_application(application)

