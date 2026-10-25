"""Optional, observational Windows pointer evidence.

This module never changes Windows gesture settings or consumes input. Qt's
ordinary mouse path can represent either a mouse or a touchpad, so IMDT_MOUSE
is deliberately *unknown*. HID button data is only used when Windows positively
identifies the matching message as touchpad input, exactly one PTP is present,
and its descriptor-defined primary button fields have fresh valid reports.
Missing data is not an unpressed button.

See Microsoft's RegisterRawInputDevices, HIDP_BUTTON_CAPS, and "Buttons, Report
Level Usages" documentation. New preview TouchpadCapable APIs are not enabled:
registering a UI thread would change all its two-finger gesture delivery.
"""

from __future__ import annotations

from collections import deque
import ctypes as C
from dataclasses import dataclass
import struct
import sys
import time
import weakref

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


PTP_PAGE, PTP_USAGE = 0x0D, 0x05
WM_INPUT, WM_INPUT_DEVICE_CHANGE = 0x00FF, 0x00FE
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
IMDT_MOUSE, IMDT_TOUCH, IMDT_TOUCHPAD = 2, 4, 16
RIDI_PREPARSEDDATA, RIDI_DEVICEINFO, RID_INPUT = 0x20000005, 0x2000000B, 0x10000003
RIDEV_REMOVE, RIDEV_PAGEONLY, RIDEV_DEVNOTIFY = 1, 0x20, 0x2000
UINT_ERROR, HIDP_SUCCESS = 0xFFFFFFFF, 0x00110000
ERROR_INSUFFICIENT_BUFFER = 122
MAX_RAW_BYTES, MAX_DEVICES, MAX_BUTTON_CAPS = 1024 * 1024, 512, 128
MAX_REPORT_BYTES, EVIDENCE_TTL, BUTTON_TTL = 4096, 0.25, 0.15


class _Point(C.Structure):
    _fields_ = [('x', C.c_int32), ('y', C.c_int32)]


class _Message(C.Structure):
    _fields_ = [('hwnd', C.c_void_p), ('message', C.c_uint32), ('wParam', C.c_size_t),
                ('lParam', C.c_ssize_t), ('time', C.c_uint32), ('pt', _Point),
                ('lPrivate', C.c_uint32)]


class _InputSource(C.Structure):
    _fields_ = [('deviceType', C.c_uint32), ('originId', C.c_uint32)]


class _RawRegistration(C.Structure):
    _fields_ = [('page', C.c_uint16), ('usage', C.c_uint16), ('flags', C.c_uint32),
                ('target', C.c_void_p)]


class _RawDevice(C.Structure):
    _fields_ = [('handle', C.c_void_p), ('kind', C.c_uint32)]


class _RawHeader(C.Structure):
    _fields_ = [('kind', C.c_uint32), ('size', C.c_uint32), ('device', C.c_void_p),
                ('parameter', C.c_size_t)]


class _RawDeviceInfo(C.Structure):
    # RID_DEVICE_INFO's largest union member is the six-DWORD keyboard info.
    _fields_ = [('size', C.c_uint32), ('kind', C.c_uint32), ('data', C.c_uint32 * 6)]


class _HidCaps(C.Structure):
    _fields_ = [('usage', C.c_uint16), ('page', C.c_uint16),
                ('input_bytes', C.c_uint16), ('output_bytes', C.c_uint16),
                ('feature_bytes', C.c_uint16), ('reserved', C.c_uint16 * 17),
                ('links', C.c_uint16), ('input_buttons', C.c_uint16),
                ('input_values', C.c_uint16), ('input_indices', C.c_uint16),
                ('output_buttons', C.c_uint16), ('output_values', C.c_uint16),
                ('output_indices', C.c_uint16), ('feature_buttons', C.c_uint16),
                ('feature_values', C.c_uint16), ('feature_indices', C.c_uint16)]


class _HidButtonCaps(C.Structure):
    # Old/new hidpi.h layouts have the same 72-byte ABI. The first DWORD of
    # Reserved[10] is ReportCount/Reserved2 in recent HID API versions.
    _fields_ = [('page', C.c_uint16), ('report_id', C.c_ubyte), ('alias', C.c_ubyte),
                ('bit_field', C.c_uint16), ('link', C.c_uint16),
                ('link_usage', C.c_uint16), ('link_page', C.c_uint16),
                ('is_range', C.c_ubyte), ('string_range', C.c_ubyte),
                ('designator_range', C.c_ubyte), ('absolute', C.c_ubyte),
                ('reserved', C.c_uint32 * 10), ('values', C.c_uint16 * 8)]


@dataclass(frozen=True)
class PointerEvidence:
    device_kind: str = 'unknown'
    physical_press: bool | None = None


@dataclass(frozen=True)
class ButtonField:
    report_id: int
    link: int
    usages: frozenset[int]


@dataclass(frozen=True)
class HidDescriptor:
    preparsed: bytes
    report_length: int
    fields: tuple[ButtonField, ...]


def primary_button_fields(caps) -> tuple[ButtonField, ...]:
    """Only descriptor-declared absolute, report-level primary buttons count."""
    groups = {}
    for cap in caps:
        if cap.page != 9 or cap.alias or not cap.absolute or cap.link != 0:
            continue
        low = int(cap.values[0])
        high = int(cap.values[1]) if cap.is_range else low
        if high < low:
            continue
        primary = frozenset(usage for usage in (1, 2) if low <= usage <= high)
        if primary:
            key = (int(cap.report_id), int(cap.link))
            groups[key] = groups.get(key, frozenset()) | primary
    return tuple(ButtonField(report, link, usages)
                 for (report, link), usages in sorted(groups.items()))


def unpack_raw_hid(data: bytes) -> tuple[int, tuple[bytes, ...]] | None:
    """Validate the entire RAWHID envelope before allowing HID parser access."""
    header_size = C.sizeof(_RawHeader)
    if not header_size + 8 <= len(data) <= MAX_RAW_BYTES:
        return None
    header = _RawHeader.from_buffer_copy(data)
    if header.kind != 2 or header.size != len(data) or not header.device:
        return None  # A zero device handle cannot supply a safe HID descriptor.
    length, count = struct.unpack_from('<II', data, header_size)
    if not 1 <= length <= MAX_REPORT_BYTES or not 1 <= count <= 512:
        return None
    if header_size + 8 + length * count != len(data):
        return None
    payload = memoryview(data)[header_size + 8:]
    return int(header.device), tuple(bytes(payload[i * length:(i + 1) * length])
                                     for i in range(count))


@dataclass(frozen=True)
class _NativeMouse:
    timestamp: int
    action: str
    root: int
    position: tuple[float, float]
    received: float
    evidence: PointerEvidence


class EvidenceLedger:
    """Bounded per-message correlation; never 'last device used' inference."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.events = deque(maxlen=48)
        self.descriptors: dict[int, HidDescriptor] = {}
        self.buttons: dict[int, dict[ButtonField, tuple[bool, float, int | None]]] = {}
        self.matches = 0

    def set_devices(self, descriptors):
        self.descriptors = dict(descriptors)
        self.buttons.clear()
        self.events.clear()

    def observe_report(self, device, report, parser, *, timestamp=None):
        descriptor = self.descriptors.get(device)
        if descriptor is None:
            return
        if len(report) != descriptor.report_length or not report:
            self.buttons.pop(device, None)
            return
        applicable = [field for field in descriptor.fields if field.report_id == report[0]]
        if not applicable:
            return  # Other contact reports cannot assert that a button is up.
        state = self.buttons.setdefault(device, {})
        try:
            decoded = [(field, bool(set(parser(descriptor, field, report)) & field.usages))
                       for field in applicable]
        except (OSError, ValueError, TypeError, OverflowError):
            self.buttons.pop(device, None)
            return
        now = self.clock()
        for field, down in decoded:
            state[field] = (down, now, timestamp & 0xFFFFFFFF if timestamp is not None else None)

    def physical_button(self, *, timestamp=None):
        # Source API identifies the device *type*, not a HID handle. More than
        # one PTP is ambiguous even if only one currently sends reports.
        if len(self.descriptors) != 1:
            return None
        device, descriptor = next(iter(self.descriptors.items()))
        if not descriptor.fields:
            return None
        states = self.buttons.get(device, {})
        now = self.clock()
        values = []
        for field in descriptor.fields:
            entry = states.get(field)
            if entry is None or not 0 <= now - entry[1] <= BUTTON_TTL:
                return None
            if timestamp is not None and entry[2] != timestamp & 0xFFFFFFFF:
                # A recent button-up report may precede a NEW physical press
                # whose raw packet is still queued. Freshness alone cannot
                # classify that new press as a tap. Require native message
                # timestamps to agree; missed/order-shifted reports fall back.
                return None
            values.append(entry[0])
        return any(values)

    def record_mouse(self, *, timestamp, action, root, position, source):
        kind = {IMDT_TOUCH: 'touchscreen', IMDT_TOUCHPAD: 'touchpad'}.get(source, 'unknown')
        physical = self.physical_button(timestamp=timestamp) if kind == 'touchpad' else None
        self.events.append(_NativeMouse(timestamp & 0xFFFFFFFF, action, root, position,
                                        self.clock(), PointerEvidence(kind, physical)))

    def lookup(self, *, timestamp, action, root, position):
        if timestamp <= 0:
            return PointerEvidence()
        now = self.clock()
        matches = [entry for entry in self.events
                   if entry.timestamp == timestamp & 0xFFFFFFFF and entry.action == action
                   and entry.root == root and 0 <= now - entry.received <= EVIDENCE_TTL
                   and max(abs(entry.position[0] - position[0]),
                           abs(entry.position[1] - position[1])) <= 1.5]
        if not matches or any(entry.evidence != matches[0].evidence for entry in matches[1:]):
            return PointerEvidence()
        self.matches += 1
        return matches[0].evidence


class WindowsInputBackend:
    """Small ctypes boundary, independently fakeable in unit tests."""
    def __init__(self, user32=None, hid=None, *, get_last_error=None):
        self.user32 = user32 or C.WinDLL('user32', use_last_error=True)
        self.hid = hid or C.WinDLL('hid', use_last_error=True)
        # ctypes exposes get_last_error only on Windows. Injection keeps the
        # documented Win32 error contract testable on other development hosts.
        self._get_last_error = get_last_error or getattr(C, 'get_last_error', lambda: 0)
        u, h = self.user32, self.hid
        specs = (
            (u.GetRegisteredRawInputDevices, [C.c_void_p, C.POINTER(C.c_uint32), C.c_uint32], C.c_uint32),
            (u.RegisterRawInputDevices, [C.POINTER(_RawRegistration), C.c_uint32, C.c_uint32], C.c_int32),
            (u.GetRawInputDeviceList, [C.c_void_p, C.POINTER(C.c_uint32), C.c_uint32], C.c_uint32),
            (u.GetRawInputDeviceInfoW, [C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
            (u.GetRawInputData, [C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32), C.c_uint32], C.c_uint32),
            (u.GetCurrentInputMessageSource, [C.POINTER(_InputSource)], C.c_int32),
            (u.GetAncestor, [C.c_void_p, C.c_uint32], C.c_void_p),
            (u.ClientToScreen, [C.c_void_p, C.POINTER(_Point)], C.c_int32),
            (u.ScreenToClient, [C.c_void_p, C.POINTER(_Point)], C.c_int32),
            (h.HidP_GetCaps, [C.c_void_p, C.POINTER(_HidCaps)], C.c_int32),
            (h.HidP_GetButtonCaps, [C.c_int32, C.c_void_p, C.POINTER(C.c_uint16), C.c_void_p], C.c_int32),
            (h.HidP_GetUsages, [C.c_int32, C.c_uint16, C.c_uint16, C.c_void_p,
                               C.POINTER(C.c_uint32), C.c_void_p, C.c_void_p, C.c_uint32], C.c_int32),
        )
        for function, arguments, result in specs:
            function.argtypes, function.restype = arguments, result

    def registrations(self):
        count = C.c_uint32()
        size = C.sizeof(_RawRegistration)
        result = self.user32.GetRegisteredRawInputDevices(None, C.byref(count), size)
        # Unlike GetRawInputDeviceList, this API documents a NULL size query
        # as -1 / ERROR_INSUFFICIENT_BUFFER, with the required count filled in.
        if result == UINT_ERROR and self._get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            raise OSError('raw-registration-query-failed')
        if count.value > MAX_DEVICES:
            raise OSError('raw-registration-limit')
        if not count.value:
            return []
        rows = (_RawRegistration * count.value)()
        result = self.user32.GetRegisteredRawInputDevices(rows, C.byref(count), size)
        if result == UINT_ERROR or result > len(rows):
            raise OSError('raw-registration-query-failed')
        return list(rows[:result])

    def register(self, target):
        if any(row.page == PTP_PAGE and (row.usage == PTP_USAGE or row.flags & RIDEV_PAGEONLY)
               for row in self.registrations()):
            return False
        row = _RawRegistration(PTP_PAGE, PTP_USAGE, RIDEV_DEVNOTIFY, target)
        if not self.user32.RegisterRawInputDevices(C.byref(row), 1, C.sizeof(row)):
            raise OSError('raw-registration-failed')
        return True

    def unregister(self, target):
        # Another component may have subsequently registered the same class.
        if not any(row.page == PTP_PAGE and row.usage == PTP_USAGE
                   and row.target == target and row.flags == RIDEV_DEVNOTIFY
                   for row in self.registrations()):
            return
        row = _RawRegistration(PTP_PAGE, PTP_USAGE, RIDEV_REMOVE, None)
        if not self.user32.RegisterRawInputDevices(C.byref(row), 1, C.sizeof(row)):
            raise OSError('raw-unregistration-failed')

    def descriptor(self, device):
        size = C.c_uint32()
        get = self.user32.GetRawInputDeviceInfoW
        if get(device, RIDI_PREPARSEDDATA, None, C.byref(size)) == UINT_ERROR:
            return None
        if not 1 <= size.value <= MAX_RAW_BYTES:
            return None
        data = C.create_string_buffer(size.value)
        result = get(device, RIDI_PREPARSEDDATA, data, C.byref(size))
        if result == UINT_ERROR or result > len(data):
            return None
        caps = _HidCaps()
        if self.hid.HidP_GetCaps(data, C.byref(caps)) != HIDP_SUCCESS:
            return None
        if (caps.page, caps.usage) != (PTP_PAGE, PTP_USAGE):
            return None
        if not 1 <= caps.input_bytes <= MAX_REPORT_BYTES or caps.input_buttons > MAX_BUTTON_CAPS:
            return None
        fields = ()
        if caps.input_buttons:
            count = C.c_uint16(caps.input_buttons)
            buttons = (_HidButtonCaps * count.value)()
            status = self.hid.HidP_GetButtonCaps(0, buttons, C.byref(count), data)
            if status == HIDP_SUCCESS and count.value <= len(buttons):
                fields = primary_button_fields(buttons[:count.value])
        return HidDescriptor(bytes(data), caps.input_bytes, fields)

    def devices(self):
        count = C.c_uint32()
        size = C.sizeof(_RawDevice)
        if self.user32.GetRawInputDeviceList(None, C.byref(count), size) == UINT_ERROR:
            raise OSError('raw-device-query-failed')
        if count.value > MAX_DEVICES:
            raise OSError('raw-device-limit')
        if not count.value:
            return {}
        rows = (_RawDevice * count.value)()
        result = self.user32.GetRawInputDeviceList(rows, C.byref(count), size)
        if result == UINT_ERROR or result > len(rows):
            raise OSError('raw-device-query-failed')
        found = {}
        for row in rows[:result]:
            if row.kind == 2 and row.handle:
                info = _RawDeviceInfo()
                info.size = C.sizeof(info)
                info_size = C.c_uint32(info.size)
                read = self.user32.GetRawInputDeviceInfoW(row.handle, RIDI_DEVICEINFO,
                                                          C.byref(info), C.byref(info_size))
                if read == UINT_ERROR or info_size.value != C.sizeof(info) or info.kind != 2:
                    # Otherwise an unreadable second PTP could be mistaken for
                    # no second device, enabling ambiguous button attribution.
                    raise OSError('raw-device-identity-unavailable')
                if (info.data[3] & 0xFFFF, info.data[3] >> 16) != (PTP_PAGE, PTP_USAGE):
                    continue
                descriptor = self.descriptor(row.handle)
                # Count an identified PTP even if its HID descriptor cannot be
                # parsed. Missing capability must never resolve ambiguity.
                found[int(row.handle)] = descriptor or HidDescriptor(b'', 0, ())
        return found

    def raw_data(self, handle):
        size = C.c_uint32()
        get = self.user32.GetRawInputData
        header_size = C.sizeof(_RawHeader)
        if get(handle, RID_INPUT, None, C.byref(size), header_size) == UINT_ERROR:
            raise OSError('raw-read-failed')
        if not header_size <= size.value <= MAX_RAW_BYTES:
            raise OSError('raw-size-invalid')
        data = C.create_string_buffer(size.value)
        result = get(handle, RID_INPUT, data, C.byref(size), header_size)
        if result == UINT_ERROR or result != len(data):
            raise OSError('raw-read-incomplete')
        return bytes(data)

    def usages(self, descriptor, field, report):
        # PTP primary button report pages are tiny; a bounded 128-entry array
        # also accommodates descriptors containing additional nonprimary bits.
        usages = (C.c_uint16 * 128)()
        count = C.c_uint32(len(usages))
        parsed = C.create_string_buffer(descriptor.preparsed)
        raw = C.create_string_buffer(report)
        status = self.hid.HidP_GetUsages(0, 9, field.link, usages, C.byref(count),
                                        parsed, raw, len(report))
        if status != HIDP_SUCCESS or count.value > len(usages):
            raise ValueError('hid-button-decode-failed')
        return set(usages[:count.value])

    def source(self):
        source = _InputSource()
        return int(source.deviceType) if self.user32.GetCurrentInputMessageSource(C.byref(source)) else 0

    def root_position(self, message):
        root = self.user32.GetAncestor(message.hwnd, 2)
        if not root:
            return None
        point = _Point(C.c_int16(message.lParam & 0xFFFF).value,
                       C.c_int16((message.lParam >> 16) & 0xFFFF).value)
        if not self.user32.ClientToScreen(message.hwnd, C.byref(point)):
            return None
        if not self.user32.ScreenToClient(root, C.byref(point)):
            return None
        return int(root), point.x, point.y


class PointerInputService(QAbstractNativeEventFilter):
    def __init__(self, application, backend=None, *, clock=time.monotonic):
        super().__init__()
        self.application = application
        self.backend = backend or WindowsInputBackend()
        self.ledger = EvidenceLedger(clock)
        self.clients = weakref.WeakSet()
        self.receiver = None
        self.target = 0
        self.closed = False
        self.status = 'initializing'
        self.errors = 0
        self.touchpad_messages = 0
        self.reports = 0
        try:
            self.receiver = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            self.receiver.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            self.receiver.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.receiver.setObjectName('LiveViewerPointerInputObserver')
            self.target = int(self.receiver.winId())
            registered = self.backend.register(self.target)
            self.status = 'registered' if registered else 'existing-registration'
            if registered:
                self.ledger.set_devices(self.backend.devices())
        except (OSError, AttributeError, ValueError, RuntimeError):
            self.errors += 1
            if self.status != 'registered':
                self.status = 'unavailable'
        application.installNativeEventFilter(self)
        application.aboutToQuit.connect(self.close)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.application.removeNativeEventFilter(self)
        if self.status == 'registered':
            try:
                self.backend.unregister(self.target)
            except (OSError, RuntimeError):
                self.errors += 1
        self.ledger.set_devices({})
        if self.receiver is not None and isValid(self.receiver):
            self.receiver.close()
            self.receiver.deleteLater()

    def _refresh_devices(self):
        # A failed hot-plug scan must not retain an obsolete single-device
        # assumption after a second touchpad was connected.
        self.ledger.set_devices({})
        self.ledger.set_devices(self.backend.devices())

    def nativeEventFilter(self, event_type, message):
        if self.closed or bytes(event_type) != b'windows_generic_MSG':
            return False, 0
        try:
            msg = _Message.from_address(int(message))
            if msg.message == WM_INPUT_DEVICE_CHANGE and self.status == 'registered':
                self._refresh_devices()
            elif msg.message == WM_INPUT and msg.hwnd == self.target and self.status == 'registered':
                raw = unpack_raw_hid(self.backend.raw_data(msg.lParam))
                if raw is None:
                    self.ledger.buttons.clear()
                else:
                    device, reports = raw
                    if device not in self.ledger.descriptors:
                        self._refresh_devices()
                    for report in reports:
                        self.ledger.observe_report(device, report, self.backend.usages, timestamp=msg.time)
                        self.reports += 1
            elif msg.message in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                # Read this inside the native callback, never later from a Qt
                # timer/handler where 'current input message' may be different.
                source = self.backend.source()
                location = self.backend.root_position(msg)
                if location is None:
                    return False, 0
                root, x, y = location
                for client in tuple(self.clients):
                    canvas = client.canvas()
                    if canvas is None or not isValid(canvas) or not canvas.isVisible():
                        continue
                    window = canvas.window()
                    if int(window.winId()) != root:
                        continue
                    ratio = window.devicePixelRatioF()
                    action = ('release' if msg.message == WM_LBUTTONUP else
                              'move' if msg.message == WM_MOUSEMOVE else 'press')
                    self.ledger.record_mouse(timestamp=msg.time, action=action, root=root,
                                             position=(x / ratio, y / ratio), source=source)
                    if source == IMDT_TOUCHPAD:
                        self.touchpad_messages += 1
                    break
        except (OSError, AttributeError, ValueError, RuntimeError, TypeError, OverflowError):
            self.errors += 1
            self.ledger.buttons.clear()
        # DefWindowProc/Qt still receives WM_INPUT and performs normal cleanup.
        return False, 0

    def diagnostics(self):
        return {'platform': sys.platform, 'raw_input': self.status,
                'ptp_devices': len(self.ledger.descriptors),
                'button_capable_devices': sum(bool(d.fields) for d in self.ledger.descriptors.values()),
                'raw_reports_observed': self.reports, 'explicit_touchpad_messages': self.touchpad_messages,
                'matched_native_messages': self.ledger.matches, 'observation_errors': self.errors,
                'physical_button_gating_requires_explicit_touchpad_source': True,
                'preview_touchpad_registration_enabled': False}


class PointerInputMonitor:
    def __init__(self, canvas, service):
        self.canvas = weakref.ref(canvas)
        self.service = service
        service.clients.add(self)

    def mouse_evidence(self, event):
        canvas = self.canvas()
        if canvas is None or not isValid(canvas) or self.service.closed:
            return PointerEvidence()
        actions = {QEvent.Type.MouseButtonPress: 'press', QEvent.Type.MouseButtonDblClick: 'press',
                   QEvent.Type.MouseButtonRelease: 'release', QEvent.Type.MouseMove: 'move'}
        action = actions.get(event.type())
        if action is None:
            return PointerEvidence()
        window = canvas.window()
        local = window.mapFromGlobal(event.globalPosition())
        return self.service.ledger.lookup(timestamp=event.timestamp(), action=action,
                                          root=int(window.winId()), position=(local.x(), local.y()))

    def diagnostics(self):
        return self.service.diagnostics()


def install_pointer_input_monitor(canvas):
    if sys.platform != 'win32':
        return None
    application = QApplication.instance()
    if application is None or application.platformName() != 'windows':
        return None  # Offscreen tests do not own valid Win32 HWNDs.
    service = getattr(application, '_liveviewer_pointer_input_service', None)
    if service is None or service.closed:
        try:
            service = PointerInputService(application)
        except (OSError, AttributeError, RuntimeError):
            return None
        application._liveviewer_pointer_input_service = service
    return PointerInputMonitor(canvas, service)
