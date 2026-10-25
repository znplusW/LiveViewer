"""Native input observations are evidence, never guesses about physical input."""
import ctypes as C
import os
import struct
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from livegallery import native_pointer_input as n


def cap(usage=1, *, high=None, report=3, page=9, link=0, absolute=True, alias=False):
    result = n._HidButtonCaps()
    result.page, result.report_id, result.link = page, report, link
    result.absolute, result.alias = absolute, alias
    result.is_range = high is not None
    result.values[0] = usage
    result.values[1] = usage if high is None else high
    return result


def packet(reports=(b'\x03\x00',), device=42, kind=2):
    payload = b''.join(reports)
    size = C.sizeof(n._RawHeader) + 8 + len(payload)
    header = n._RawHeader(kind, size, device, 0)
    return bytes(header) + struct.pack('<II', len(reports[0]), len(reports)) + payload


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.ledger = n.EvidenceLedger(lambda: self.now)
        self.field = n.ButtonField(3, 0, frozenset({1}))
        self.descriptor = n.HidDescriptor(b'pretend-preparsed', 2, (self.field,))
        self.ledger.set_devices({42: self.descriptor})

    @staticmethod
    def parse(descriptor, field, report):
        return {1} if report[1] else set()

    def observe(self, down=False, timestamp=100):
        self.ledger.observe_report(42, bytes((3, int(down))), self.parse, timestamp=timestamp)

    def record(self, source=n.IMDT_TOUCHPAD, timestamp=100, action='press', root=50, position=(10., 20.)):
        self.ledger.record_mouse(timestamp=timestamp, action=action, root=root, position=position, source=source)

    def lookup(self, timestamp=100, action='press', root=50, position=(10., 20.)):
        return self.ledger.lookup(timestamp=timestamp, action=action, root=root, position=position)

    def test_raw_hid_uses_64_bit_windows_header_and_validates_full_packet(self):
        self.assertEqual(C.sizeof(n._RawHeader), 24)
        self.assertEqual(n.unpack_raw_hid(packet((b'\x03\x00', b'\x03\x01'))),
                         (42, (b'\x03\x00', b'\x03\x01')))
        for bad in (b'', packet()[:-1], packet() + b'\0', packet(device=0), packet(kind=0)):
            self.assertIsNone(n.unpack_raw_hid(bad))
        malformed = bytearray(packet())
        struct.pack_into('<II', malformed, 24, 0xFFFFFFFF, 0xFFFFFFFF)
        self.assertIsNone(n.unpack_raw_hid(bytes(malformed)))

    def test_button_caps_windows_abi_and_primary_mapping(self):
        self.assertEqual(C.sizeof(n._HidCaps), 64)
        self.assertEqual(C.sizeof(n._HidButtonCaps), 72)
        self.assertEqual(n._HidButtonCaps.values.offset, 56)
        self.assertEqual(C.sizeof(n._RawRegistration), 16)
        fields = n.primary_button_fields([cap(), cap(2), cap(3), cap(1, high=3, report=4),
                                          cap(page=13), cap(link=1), cap(absolute=False), cap(alias=True)])
        self.assertEqual(fields, (n.ButtonField(3, 0, frozenset({1, 2})),
                                  n.ButtonField(4, 0, frozenset({1, 2}))))

    def test_missing_optional_buttons_and_no_report_are_unknown(self):
        self.assertIsNone(self.ledger.physical_button())
        self.ledger.set_devices({42: n.HidDescriptor(b'', 2, ())})
        self.observe()
        self.assertIsNone(self.ledger.physical_button())

    def test_fresh_explicit_touchpad_source_can_use_declared_button_state(self):
        for down in (False, True):
            self.observe(down)
            self.record()
            self.assertEqual(self.lookup(), n.PointerEvidence('touchpad', down))
            self.ledger.events.clear()

    def test_generic_mouse_is_not_inferred_to_be_touchpad_from_recent_hid(self):
        self.observe(False)
        self.record(source=n.IMDT_MOUSE)
        self.assertEqual(self.lookup(), n.PointerEvidence())
        self.record(source=n.IMDT_MOUSE, timestamp=101)
        self.assertIsNone(self.lookup(timestamp=101).physical_press)

    def test_touchscreen_never_inherits_touchpad_buttons(self):
        self.observe(True)
        self.record(source=n.IMDT_TOUCH)
        self.assertEqual(self.lookup(), n.PointerEvidence('touchscreen'))

    def test_stale_buttons_are_unknown_while_source_remains_known(self):
        self.observe(False)
        self.now += n.BUTTON_TTL + .001
        self.record()
        self.assertEqual(self.lookup(), n.PointerEvidence('touchpad'))

    def test_fresh_previous_button_up_cannot_suppress_new_physical_press(self):
        self.observe(False, timestamp=99)
        self.record(timestamp=100)
        self.assertEqual(self.lookup(), n.PointerEvidence('touchpad'))
        # The packet belonging to this new press arrives after the mouse
        # message; don't retroactively relabel that already-dispatched event.
        self.observe(True, timestamp=100)
        self.assertEqual(self.lookup(), n.PointerEvidence('touchpad'))

    def test_timestamp_missing_or_mismatched_raw_report_never_means_unpressed(self):
        self.observe(False, timestamp=None)
        self.record()
        self.assertIsNone(self.lookup().physical_press)

    def test_multiple_ptp_devices_are_ambiguous(self):
        self.ledger.set_devices({42: self.descriptor, 43: self.descriptor})
        self.observe(False)
        self.record()
        self.assertEqual(self.lookup(), n.PointerEvidence('touchpad'))

    def test_invalid_report_clears_prior_button_observation(self):
        self.observe(False)
        self.ledger.observe_report(42, b'\x03', self.parse)
        self.assertIsNone(self.ledger.physical_button())
        self.observe(False)
        def fail(*args):
            raise ValueError('bad-HID')
        self.ledger.observe_report(42, b'\x03\0', fail)
        self.assertIsNone(self.ledger.physical_button())

    def test_other_reports_do_not_assume_button_released(self):
        self.observe(True)
        self.ledger.observe_report(42, b'\x04\x00', self.parse)
        self.assertTrue(self.ledger.physical_button())

    def test_split_button_reports_require_all_primary_fields_fresh(self):
        other = n.ButtonField(4, 0, frozenset({2}))
        self.ledger.set_devices({42: n.HidDescriptor(b'', 2, (self.field, other))})
        self.observe(False)
        self.assertIsNone(self.ledger.physical_button())
        self.ledger.observe_report(42, b'\x04\0', self.parse)
        self.assertFalse(self.ledger.physical_button())

    def test_message_match_requires_timestamp_action_window_and_position(self):
        self.observe(False)
        self.record()
        for args in ({'timestamp': 0}, {'timestamp': 101}, {'root': 51},
                     {'action': 'release'}, {'position': (14., 20.)}):
            self.assertEqual(self.lookup(**args), n.PointerEvidence())
        self.assertEqual(self.lookup(), n.PointerEvidence('touchpad', False))
        self.now += n.EVIDENCE_TTL + .001
        self.assertEqual(self.lookup(), n.PointerEvidence())

    def test_conflicting_same_tick_events_are_unknown_not_last_event_wins(self):
        self.observe(False)
        self.record()
        self.record(source=n.IMDT_MOUSE)
        self.assertEqual(self.lookup(), n.PointerEvidence())

    def test_timestamp_wrap_and_bounded_queue(self):
        self.record(timestamp=(1 << 32) + 100)
        self.assertEqual(self.lookup(), n.PointerEvidence('touchpad'))
        for i in range(100):
            self.record(timestamp=200 + i)
        self.assertEqual(len(self.ledger.events), 48)


class Function:
    def __init__(self, callback):
        self.callback = callback
    def __call__(self, *args):
        return self.callback(*args)


class FakeApi:
    def __init__(self):
        self.registrations = []
        self.registration_calls = []
        self.last_error = 0
        self.caps = n._HidCaps()
        self.caps.page, self.caps.usage, self.caps.input_bytes, self.caps.input_buttons = 13, 5, 2, 1
        self.button_caps = [cap()]
        self.GetRegisteredRawInputDevices = Function(self.get_registrations)
        self.RegisterRawInputDevices = Function(self.register)
        self.GetRawInputDeviceList = Function(lambda *args: 0)
        self.GetRawInputDeviceInfoW = Function(self.get_info)
        self.GetRawInputData = Function(lambda *args: n.UINT_ERROR)
        self.GetCurrentInputMessageSource = Function(lambda *args: 0)
        self.GetAncestor = Function(lambda hwnd, kind: hwnd)
        self.ClientToScreen = Function(lambda *args: 1)
        self.ScreenToClient = Function(lambda *args: 1)
        self.HidP_GetCaps = Function(self.get_caps)
        self.HidP_GetButtonCaps = Function(self.get_buttons)
        self.HidP_GetUsages = Function(self.get_usages)

    def get_registrations(self, rows, count, size):
        C.cast(count, C.POINTER(C.c_uint32))[0] = len(self.registrations)
        if rows is not None:
            for i, row in enumerate(self.registrations):
                rows[i] = row
            return len(self.registrations)
        self.last_error = n.ERROR_INSUFFICIENT_BUFFER
        return n.UINT_ERROR

    def register(self, row, count, size):
        actual = C.cast(row, C.POINTER(n._RawRegistration))[0]
        self.registration_calls.append((actual.page, actual.usage, actual.flags, actual.target))
        self.registrations = ([] if actual.flags == n.RIDEV_REMOVE else
                              [n._RawRegistration(actual.page, actual.usage, actual.flags, actual.target)])
        return 1

    def get_info(self, device, command, data, size):
        if command == n.RIDI_DEVICEINFO:
            info = C.cast(data, C.POINTER(n._RawDeviceInfo))[0]
            info.kind = 2
            info.data[3] = 13 | (5 << 16)
            C.cast(size, C.POINTER(C.c_uint32))[0] = C.sizeof(info)
            return C.sizeof(info)
        C.cast(size, C.POINTER(C.c_uint32))[0] = 4
        if data is not None:
            C.memmove(data, b'HID!', 4)
            return 4
        return 0

    def get_caps(self, parsed, caps):
        C.memmove(caps, C.byref(self.caps), C.sizeof(self.caps))
        return n.HIDP_SUCCESS

    def get_buttons(self, kind, rows, count, data):
        for i, row in enumerate(self.button_caps):
            rows[i] = row
        C.cast(count, C.POINTER(C.c_uint16))[0] = len(self.button_caps)
        return n.HIDP_SUCCESS

    def get_usages(self, kind, page, link, usages, count, parsed, report, length):
        down = C.string_at(report, length)[1]
        C.cast(count, C.POINTER(C.c_uint32))[0] = int(bool(down))
        if down:
            usages[0] = 1
        return n.HIDP_SUCCESS


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.backend = n.WindowsInputBackend(self.api, self.api,
                                             get_last_error=lambda: self.api.last_error)

    def test_null_registration_query_accepts_only_documented_size_error(self):
        self.api.registrations = [n._RawRegistration(13, 5, n.RIDEV_DEVNOTIFY, 999)]
        rows = self.backend.registrations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target, 999)
        self.assertEqual(self.api.last_error, n.ERROR_INSUFFICIENT_BUFFER)

    def test_registration_query_real_failure_never_overwrites_other_owner(self):
        def failed_query(rows, count, size):
            C.cast(count, C.POINTER(C.c_uint32))[0] = 1
            self.api.last_error = 5  # ERROR_ACCESS_DENIED is not a size query.
            return n.UINT_ERROR
        self.api.GetRegisteredRawInputDevices.callback = failed_query
        with self.assertRaisesRegex(OSError, 'raw-registration-query-failed'):
            self.backend.register(100)
        self.assertEqual(self.api.registration_calls, [])

    def test_registration_buffer_growth_between_queries_fails_closed(self):
        def growing_query(rows, count, size):
            C.cast(count, C.POINTER(C.c_uint32))[0] = 1 if rows is None else 2
            self.api.last_error = n.ERROR_INSUFFICIENT_BUFFER
            return n.UINT_ERROR
        self.api.GetRegisteredRawInputDevices.callback = growing_query
        with self.assertRaisesRegex(OSError, 'raw-registration-query-failed'):
            self.backend.register(100)
        self.assertEqual(self.api.registration_calls, [])

    def test_registration_is_front_only_nonexclusive_and_owner_checked(self):
        self.assertTrue(self.backend.register(100))
        self.assertEqual(self.api.registration_calls, [(13, 5, n.RIDEV_DEVNOTIFY, 100)])
        self.assertFalse(self.backend.register(101))
        self.backend.unregister(101)
        self.assertEqual(len(self.api.registration_calls), 1)
        self.backend.unregister(100)
        self.assertEqual(self.api.registration_calls[-1], (13, 5, n.RIDEV_REMOVE, None))

    def test_existing_whole_digitizer_page_registration_is_not_overwritten(self):
        self.api.registrations = [n._RawRegistration(13, 0, n.RIDEV_PAGEONLY, 999)]
        self.assertFalse(self.backend.register(100))
        self.assertEqual(self.api.registration_calls, [])

    def test_real_ctypes_descriptor_and_hid_parser_boundaries(self):
        descriptor = self.backend.descriptor(42)
        self.assertEqual(descriptor.preparsed, b'HID!')
        self.assertEqual(descriptor.fields, (n.ButtonField(3, 0, frozenset({1})),))
        self.assertEqual(self.backend.usages(descriptor, descriptor.fields[0], b'\x03\x00'), set())
        self.assertEqual(self.backend.usages(descriptor, descriptor.fields[0], b'\x03\x01'), {1})

    def test_wrong_collection_and_missing_buttons_do_not_enable_gating(self):
        self.api.caps.usage = 4
        self.assertIsNone(self.backend.descriptor(42))
        self.api.caps.usage, self.api.caps.input_buttons = 5, 0
        self.assertEqual(self.backend.descriptor(42).fields, ())

    def test_failed_hid_parse_does_not_become_button_up(self):
        descriptor = self.backend.descriptor(42)
        self.api.HidP_GetUsages.callback = lambda *args: -1
        with self.assertRaises(ValueError):
            self.backend.usages(descriptor, descriptor.fields[0], b'\x03\0')

    def test_unreadable_second_ptp_descriptor_still_counts_as_device(self):
        def listing(rows, count, size):
            C.cast(count, C.POINTER(C.c_uint32))[0] = 2
            if rows is not None:
                rows[0], rows[1] = n._RawDevice(42, 2), n._RawDevice(43, 2)
                return 2
            return 0
        self.api.GetRawInputDeviceList.callback = listing
        original = self.backend.descriptor
        with patch.object(self.backend, 'descriptor', side_effect=lambda device: original(device) if device == 42 else None):
            devices = self.backend.devices()
        self.assertEqual(set(devices), {42, 43})
        self.assertFalse(devices[43].fields)
        ledger = n.EvidenceLedger()
        ledger.set_devices(devices)
        self.assertIsNone(ledger.physical_button())


class FakeBackend:
    def __init__(self, registered=True):
        self.registered = registered
        self.calls = []
        self.source_type = n.IMDT_TOUCHPAD
        self.raw = packet()
    def register(self, target):
        self.calls.append(('register', target))
        return self.registered
    def unregister(self, target):
        self.calls.append(('unregister', target))
    def devices(self):
        return {42: n.HidDescriptor(b'', 2, (n.ButtonField(3, 0, frozenset({1})),))}
    def raw_data(self, handle):
        return self.raw
    def usages(self, descriptor, field, report):
        return {1} if report[1] else set()
    def source(self):
        return self.source_type
    def root_position(self, message):
        return int(message.hwnd), 30, 40


class NativeServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = QWidget()
        self.canvas.resize(300, 200)
        self.canvas.show()
        self.app.processEvents()
        self.backend = FakeBackend()
        self.service = n.PointerInputService(self.app, self.backend)
        self.monitor = n.PointerInputMonitor(self.canvas, self.service)

    def tearDown(self):
        self.service.close()
        self.canvas.close()
        self.canvas.deleteLater()
        self.app.processEvents()

    def send_native(self, kind, *, hwnd=None, timestamp=123):
        msg = n._Message(hwnd or int(self.canvas.winId()), kind, 0, 42, timestamp, n._Point(), 0)
        return self.service.nativeEventFilter(b'windows_generic_MSG', C.addressof(msg))

    def test_observation_never_consumes_input_and_matches_qt_event(self):
        self.assertEqual(self.send_native(n.WM_INPUT, hwnd=self.service.target), (False, 0))
        self.assertEqual(self.send_native(n.WM_LBUTTONDOWN), (False, 0))
        local = QPointF(30, 40) / self.canvas.devicePixelRatioF()
        event = QMouseEvent(QEvent.Type.MouseButtonPress, local, self.canvas.mapToGlobal(local),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        event.setTimestamp(123)
        self.assertEqual(self.monitor.mouse_evidence(event), n.PointerEvidence('touchpad', False))
        event.setTimestamp(124)
        self.assertEqual(self.monitor.mouse_evidence(event), n.PointerEvidence())

    def test_callback_failure_clears_buttons_but_preserves_input_delivery(self):
        self.send_native(n.WM_INPUT, hwnd=self.service.target)
        self.assertFalse(self.service.ledger.physical_button())
        self.backend.raw_data = lambda handle: (_ for _ in ()).throw(OSError('lost device'))
        self.assertEqual(self.send_native(n.WM_INPUT, hwnd=self.service.target), (False, 0))
        self.assertIsNone(self.service.ledger.physical_button())
        self.assertEqual(self.monitor.diagnostics()['observation_errors'], 1)

    def test_failed_hotplug_scan_cannot_retain_single_device_assumption(self):
        self.send_native(n.WM_INPUT, hwnd=self.service.target)
        self.backend.devices = lambda: (_ for _ in ()).throw(OSError('hotplug race'))
        self.assertEqual(self.send_native(n.WM_INPUT_DEVICE_CHANGE), (False, 0))
        self.assertEqual(self.service.ledger.descriptors, {})
        self.assertIsNone(self.service.ledger.physical_button())

    def test_existing_registration_is_not_read_or_unregistered(self):
        other = n.PointerInputService(self.app, FakeBackend(registered=False))
        try:
            self.assertEqual(other.diagnostics()['raw_input'], 'existing-registration')
            self.assertEqual(other.ledger.descriptors, {})
        finally:
            other.close()
        self.assertEqual(len(other.backend.calls), 1)

    def test_close_is_idempotent_and_retires_input_evidence(self):
        self.service.close()
        self.service.close()
        self.assertEqual([row[0] for row in self.backend.calls], ['register', 'unregister'])
        self.assertEqual(self.service.ledger.descriptors, {})

    def test_non_native_platform_does_not_register_raw_input(self):
        if self.app.platformName() != 'windows':
            with patch.object(n.sys, 'platform', 'win32'), patch.object(n, 'WindowsInputBackend') as backend:
                self.assertIsNone(n.install_pointer_input_monitor(self.canvas))
                backend.assert_not_called()


if __name__ == '__main__':
    unittest.main()
