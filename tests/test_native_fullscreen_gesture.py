"""Window-store ABI, safe ownership, cleanup, and actual qwindows readback."""
import ctypes
import os
import sys
import json
import subprocess
import textwrap
import unittest
from contextlib import contextmanager
from unittest.mock import patch, Mock
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget
from livegallery.fullscreen_edge_gesture import FullscreenEdgeGesture
from livegallery.native_fullscreen_gesture import (COOKIE_NAME, EDGE_PROPERTY, PROPERTY_GUID,
    IID_IPROPERTYSTORE, NativeWindowGestureStore, PROPVARIANT, PROPERTYKEY,
    VT_BOOL, VT_EMPTY, VT_LPWSTR, VARIANT_TRUE, WindowEdgeGesturePolicy)


class FakeBackend:
    def __init__(self):
        self.cookies, self.values, self.writes = {}, {}, []
        self.fail_set = self.fail_clear = False

    def claim(self, hwnd, cookie):
        self.cookies[hwnd] = cookie

    def matches(self, hwnd, cookie):
        return self.cookies.get(hwnd) == cookie

    def release(self, hwnd, cookie):
        if self.matches(hwnd, cookie):
            del self.cookies[hwnd]

    def write(self, hwnd, value):
        if self.fail_set and value is True or self.fail_clear and value is None:
            raise OSError('injected API failure')
        self.writes.append((hwnd, value))
        actual = {'vt': VT_EMPTY, 'value': None} if value is None else {'vt': VT_BOOL, 'value': True}
        self.values[hwnd] = actual
        return actual


class PropertyStoreAbiTests(unittest.TestCase):
    def test_exact_key_iid_and_x64_variant_layout(self):
        import uuid
        self.assertEqual(bytes(EDGE_PROPERTY.fmtid), uuid.UUID(PROPERTY_GUID).bytes_le)
        self.assertEqual(EDGE_PROPERTY.pid, 2)
        self.assertEqual(bytes(IID_IPROPERTYSTORE), uuid.UUID('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99').bytes_le)
        self.assertEqual(ctypes.sizeof(PROPERTYKEY), 20)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(PROPVARIANT), 24)
            self.assertEqual(ctypes.alignment(PROPVARIANT), 8)
        self.assertEqual(PROPVARIANT.value.offset, 8)

    def test_get_set_slots_bool_empty_and_clear_every_read_no_commit(self):
        calls, cleared = [], []

        class Store:
            current = (VT_EMPTY, 0)
            def call(self, slot, argument_types, key, value):
                self_outer.assertEqual(ctypes.cast(key, ctypes.POINTER(PROPERTYKEY)).contents.pid, 2)
                variant = ctypes.cast(value, ctypes.POINTER(PROPVARIANT)).contents
                calls.append(slot)
                if slot == 6:
                    self.current = variant.vt, variant.boolVal
                    if variant.vt == VT_BOOL:
                        self_outer.assertEqual(variant.boolVal, VARIANT_TRUE)
                elif slot == 5:
                    variant.vt, variant.boolVal = self.current
                else:
                    self_outer.fail('Unexpected COM slot, including an unnecessary Commit')
                return 0
        self_outer = self
        store = Store()
        class Ole:
            def PropVariantClear(self, value):
                cleared.append(ctypes.cast(value, ctypes.POINTER(PROPVARIANT)).contents.vt)
                return 0
        @contextmanager
        def opened(hwnd):
            self.assertEqual(hwnd, 123)
            yield store, Ole()
        native = object.__new__(NativeWindowGestureStore)
        with patch.object(native, 'is_own_window', return_value=True), \
                patch('livegallery.native_fullscreen_gesture._property_store', opened):
            self.assertEqual(native.write(123, True), {'vt': VT_BOOL, 'value': True})
            self.assertEqual(native.read(123), {'vt': VT_BOOL, 'value': True})
            self.assertEqual(native.write(123, None), {'vt': VT_EMPTY, 'value': None})
        self.assertEqual(calls, [6, 5, 5, 6, 5])
        self.assertEqual(cleared, [VT_BOOL, VT_BOOL, VT_EMPTY])

    def test_failed_get_still_clears_propvariant_and_foreign_hwnd_is_rejected(self):
        calls = []
        class Store:
            def call(self, *args):
                return 0x80004005
        class Ole:
            def PropVariantClear(self, value):
                calls.append(True)
        with self.assertRaises(OSError):
            NativeWindowGestureStore._read(Store(), Ole())
        self.assertEqual(calls, [True])
        native = object.__new__(NativeWindowGestureStore)
        with patch.object(native, 'is_own_window', return_value=False), \
                patch('livegallery.native_fullscreen_gesture._property_store') as opened:
            with self.assertRaises(OSError): native.read(123)
            with self.assertRaises(OSError): native.write(123, None)
            opened.assert_not_called()

    def test_cookie_remove_readback_failure_is_not_reported_as_success(self):
        native = object.__new__(NativeWindowGestureStore)
        native.user = SimpleNamespace(RemovePropW=Mock(return_value=0))
        with patch.object(native, 'matches', return_value=True):
            with self.assertRaises(OSError): native.release(123, 999)
        native.user.RemovePropW.assert_called_once_with(123, COOKIE_NAME)

    def read_string(self, text, conversion):
        buffer = ctypes.create_unicode_buffer(text)
        cleared, diagnostic = [], {}

        class Store:
            def call(self, slot, argument_types, key, pointer):
                self_outer.assertEqual(slot, 5)
                value = ctypes.cast(pointer, ctypes.POINTER(PROPVARIANT)).contents
                value.vt, value.pointer = VT_LPWSTR, ctypes.cast(buffer, ctypes.c_void_p).value
                return 1  # Successful GetValue canonicalization, observed on Windows.

        class Ole:
            def PropVariantClear(self, pointer):
                cleared.append(ctypes.cast(pointer, ctypes.POINTER(PROPVARIANT)).contents.vt)
                ctypes.memset(pointer, 0, ctypes.sizeof(PROPVARIANT))

        self_outer = self
        with patch('livegallery.native_fullscreen_gesture._variant_to_boolean', return_value=conversion) as convert:
            try:
                actual = NativeWindowGestureStore._read(Store(), Ole(), diagnostic)
            finally:
                self.assertEqual(cleared, [VT_LPWSTR])
                convert.assert_called_once()
        return actual, diagnostic

    def test_canonical_string_true_uses_successful_windows_conversion_and_records_raw_value(self):
        actual, diagnostic = self.read_string('true', (0, True))
        self.assertEqual(actual, {'vt': VT_BOOL, 'value': True})
        self.assertEqual(diagnostic, {'get_hresult': '0x00000001', 'raw_vt': VT_LPWSTR,
            'raw_value': 'true', 'conversion_hresult': '0x00000000', 'conversion_succeeded': True,
            'canonical_vt': VT_BOOL, 'value': True})

    def test_nonempty_string_false_is_false_and_never_python_truthiness(self):
        actual, diagnostic = self.read_string('false', (0, False))
        self.assertEqual(actual, {'vt': VT_BOOL, 'value': False})
        self.assertEqual(diagnostic['raw_value'], 'false')
        self.assertTrue(diagnostic['conversion_succeeded'])

    def test_invalid_string_conversion_fails_and_still_clears_native_resource(self):
        with self.assertRaisesRegex(OSError, '0x80070057'):
            self.read_string('invalid Boolean', (0x80070057, False))

    def test_false_canonical_string_cannot_pass_write_true_verification(self):
        buffer = ctypes.create_unicode_buffer('false')
        clears = []

        class Store:
            def call(self, slot, argument_types, key, pointer):
                if slot == 5:
                    value = ctypes.cast(pointer, ctypes.POINTER(PROPVARIANT)).contents
                    value.vt, value.pointer = VT_LPWSTR, ctypes.cast(buffer, ctypes.c_void_p).value
                return 0

        class Ole:
            def PropVariantClear(self, pointer):
                clears.append(True)

        @contextmanager
        def opened(hwnd):
            yield Store(), Ole()

        native = object.__new__(NativeWindowGestureStore)
        with patch.object(native, 'is_own_window', return_value=True), \
                patch('livegallery.native_fullscreen_gesture._property_store', opened), \
                patch('livegallery.native_fullscreen_gesture._variant_to_boolean', return_value=(0, False)):
            with self.assertRaisesRegex(OSError, 'readback mismatch'):
                native.write(123, True)
        self.assertEqual(clears, [True])
        self.assertIs(native.last_readback['value'], False)

    def test_propsys_conversion_uses_32_bit_bool_and_checks_raw_hresult(self):
        from livegallery.native_fullscreen_gesture import _variant_to_boolean
        value = PROPVARIANT()
        value.vt = VT_LPWSTR

        def called(pointer, output):
            self.assertEqual(ctypes.cast(pointer, ctypes.POINTER(PROPVARIANT)).contents.vt, VT_LPWSTR)
            ctypes.cast(output, ctypes.POINTER(ctypes.c_int32)).contents.value = 1
            return 0

        function = Mock(side_effect=called)
        with patch('livegallery.native_fullscreen_gesture.ctypes.WinDLL', create=True,
                   return_value=SimpleNamespace(PropVariantToBoolean=function)) as opened:
            self.assertEqual(_variant_to_boolean(value), (0, True))
        opened.assert_called_once_with('propsys.dll', winmode=0x800)
        self.assertEqual(function.argtypes, [ctypes.POINTER(PROPVARIANT), ctypes.POINTER(ctypes.c_int32)])
        self.assertEqual(function.restype, ctypes.c_int32)

    def test_other_property_types_are_rejected_and_cleared(self):
        clears = []

        class Store:
            def call(self, slot, argument_types, key, pointer):
                ctypes.cast(pointer, ctypes.POINTER(PROPVARIANT)).contents.vt = 3
                return 0

        class Ole:
            def PropVariantClear(self, pointer):
                clears.append(True)

        diagnostic = {}
        with patch('livegallery.native_fullscreen_gesture._variant_to_boolean') as convert:
            with self.assertRaisesRegex(OSError, 'Unexpected.*type: 3'):
                NativeWindowGestureStore._read(Store(), Ole(), diagnostic)
            convert.assert_not_called()
        self.assertEqual(clears, [True])
        self.assertFalse(diagnostic['conversion_succeeded'])


class WindowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.windows = [self.window]
        self.edge = FullscreenEdgeGesture(lambda: self.windows)
        self.edge.native_policy.close()
        self.backend = FakeBackend()
        self.policy = self.edge.native_policy = WindowEdgeGesturePolicy(
            lambda: self.windows, backend=self.backend, supported=True, parent=self.edge)
        self.policy.set_enabled(True)

    def tearDown(self):
        self.edge.close()
        from shiboken6 import isValid
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()

    def fullscreen(self):
        self.window.showFullScreen()
        self.app.processEvents()
        self.policy.sync_now()
        return int(self.window.winId())

    def test_only_real_fullscreen_enabled_windows_set_and_exit_or_empty_removes(self):
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.backend.writes, [])
        hwnd = self.fullscreen()
        self.assertEqual(self.backend.values[hwnd], {'vt': VT_BOOL, 'value': True})
        self.policy.sync_now()
        self.assertEqual(self.backend.writes.count((hwnd, True)), 1)
        self.window.showNormal()
        self.app.processEvents()
        self.assertEqual(self.backend.values[hwnd]['vt'], VT_EMPTY)
        self.assertNotIn(hwnd, self.backend.cookies)
        self.fullscreen()
        self.edge.set_enabled_edges([])
        self.assertEqual(self.backend.values[hwnd]['vt'], VT_EMPTY)
        self.assertNotIn(hwnd, self.backend.cookies)
        self.assertFalse(self.policy.report()['native_api_used'])  # Mock never claims native success.

    def test_provider_forgot_preview_but_close_still_clears_tracked_native_property(self):
        hwnd = self.fullscreen()
        self.windows.clear()
        self.window.close()
        self.assertEqual(self.backend.values[hwnd]['vt'], VT_EMPTY)
        self.assertNotIn(hwnd, self.backend.cookies)
        self.assertEqual(self.policy._tracked, {})

    def test_cookie_prevents_clearing_recycled_hwnd_even_in_same_process(self):
        hwnd = self.fullscreen()
        self.backend.cookies[hwnd] += 123
        writes = list(self.backend.writes)
        self.policy.set_enabled(False)
        self.assertEqual(self.backend.writes, writes)
        self.assertEqual(self.backend.values[hwnd]['vt'], VT_BOOL)
        self.assertIn(hwnd, self.backend.cookies)
        self.assertTrue(any(item['action'] == 'stale_handle_skipped' for item in self.policy.report()['diagnostics']))

    def test_winid_change_clears_old_valid_identity_then_sets_new(self):
        old = self.fullscreen()
        with patch.object(self.window, 'winId', return_value=old + 100):
            self.policy.observe(self.window, QEvent(QEvent.Type.WinIdChange))
            self.policy.sync_now()
            self.assertEqual(self.backend.values[old]['vt'], VT_EMPTY)
            self.assertEqual(self.backend.values[old + 100]['vt'], VT_BOOL)
        self.policy.close()
        self.assertEqual(self.backend.values[old + 100]['vt'], VT_EMPTY)

    def test_failures_are_diagnostic_retryable_and_shutdown_cleans(self):
        self.backend.fail_set = True
        hwnd = self.fullscreen()
        self.assertTrue(self.policy.report()['failures'])
        self.backend.fail_set = False
        self.policy.sync_now()
        self.assertEqual(self.backend.values[hwnd]['vt'], VT_BOOL)
        self.backend.fail_clear = True
        self.policy.set_enabled(False)
        self.assertTrue(self.policy._tracked)
        self.backend.fail_clear = False
        self.policy.close()

    def test_cookie_cleanup_failure_keeps_ownership_for_retry(self):
        hwnd = self.fullscreen()
        with patch.object(self.backend, 'release', side_effect=OSError('RemoveProp failed')):
            self.policy.set_enabled(False)
        self.assertTrue(self.policy._tracked)
        self.assertTrue(self.policy.report()['failures'])
        self.policy.sync_now()
        self.assertNotIn(hwnd, self.backend.cookies)
        self.assertFalse(self.policy._tracked)
        self.assertEqual(self.backend.values[hwnd]['vt'], VT_EMPTY)
        self.assertNotIn(hwnd, self.backend.cookies)
        self.policy.close()

    def test_policy_readback_history_copies_the_raw_diagnostic(self):
        self.backend.last_readback = {'raw_vt': VT_LPWSTR, 'raw_value': 'true', 'value': True}
        self.fullscreen()
        self.backend.last_readback['raw_value'] = 'changed after read'
        records = [entry for entry in self.policy.report()['diagnostics'] if entry['action'] == 'set']
        self.assertEqual(records[-1]['readback']['raw_value'], 'true')

    def test_close_observer_verifies_real_clear_before_dead_or_surviving_handle(self):
        from tests.native_edge_probe import observe_native_close

        class ObservedBackend(FakeBackend):
            alive = True

            def is_own_window(self, hwnd):
                return self.alive

            def write(self, hwnd, value):
                actual = super().write(hwnd, value)
                self.last_readback = {'raw_vt': actual['vt'], 'canonical_vt': actual['vt'],
                                      'value': actual['value']}
                return actual

            def read(self, hwnd):
                if not self.alive:
                    raise OSError('Native window is no longer owned')
                self.last_readback = {'raw_vt': self.values[hwnd]['vt']}
                return self.values[hwnd]

        for destroyed in (False, True):
            with self.subTest(old_hwnd_destroyed=destroyed):
                self.backend = ObservedBackend()
                self.policy._backend = self.backend
                self.windows[:] = [self.window]
                self.fullscreen()
                original_close = self.window.close

                def close():
                    result = original_close()  # Real Qt Close dispatches the policy cleanup.
                    self.backend.alive = not destroyed
                    return result

                diagnostic = {}
                with patch.object(self.window, 'close', side_effect=close):
                    actual = observe_native_close(self.window, self.windows, self.policy, diagnostic)
                self.assertEqual(actual, {'vt': VT_EMPTY, 'value': None})
                self.assertTrue(diagnostic['cleanup_observed_before_destroy'])
                self.assertEqual(diagnostic['old_hwnd_destroyed'], destroyed)
                self.assertEqual(diagnostic['native_dead_handle_rejected'], destroyed)
                self.assertTrue(diagnostic['writes'][0]['owned_before'])
                self.assertEqual(diagnostic['releases'], [{'owned_after': True, 'cookie_matches_after': False}])

    def test_unsupported_platform_does_not_load_native_api_or_create_hwnd(self):
        hidden = QWidget()
        policy = WindowEdgeGesturePolicy(lambda: [hidden], supported=False)
        try:
            with patch('livegallery.native_fullscreen_gesture.NativeWindowGestureStore') as backend:
                policy.set_enabled(True)
                policy.sync_now()
                backend.assert_not_called()
            self.assertFalse(hidden.testAttribute(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.WidgetAttribute.WA_WState_Created))
            self.assertFalse(policy.report()['supported'])
            self.assertFalse(policy.report()['native_api_used'])
        finally:
            policy.close()
            hidden.deleteLater()


@unittest.skipUnless(sys.platform == 'win32',
                     'Actual HWND property store requires native qwindows')
class NativeReadbackTests(unittest.TestCase):
    def test_actual_hwnd_set_get_bool_true_and_vt_empty_cleanup(self):
        script = textwrap.dedent('''
            import json, os, sys, traceback
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QApplication, QWidget
            from livegallery.fullscreen_edge_gesture import FullscreenEdgeGesture
            from livegallery.native_fullscreen_gesture import NativeWindowGestureStore, VT_BOOL, VT_EMPTY
            from tests.native_edge_probe import observe_native_close
            app=QApplication([])
            assert app.platformName()=='windows'
            errors=[]
            def report_error(kind,value,tb):
                errors.append(f'{kind.__name__}: {value}')
                traceback.print_exception(kind,value,tb)
            sys.excepthook=report_error
            window=QWidget(); windows=[window]
            edge=FullscreenEdgeGesture(lambda:windows)
            native=NativeWindowGestureStore()
            raw=[]
            phase='created'
            def require_true(hwnd):
                assert native.read(hwnd)=={'vt':VT_BOOL,'value':True}, native.last_readback
                record=dict(native.last_readback);raw.append(record)
                assert record['raw_vt'] in (11,31),record
                assert record['conversion_succeeded'] is True,record
                assert record['canonical_vt']==11 and record['value'] is True,record
            try:
                phase='initial_fullscreen'
                window.showFullScreen(); app.processEvents(); edge.native_policy.sync_now()
                hwnd=int(window.winId())
                require_true(hwnd)
                assert edge.native_policy.report()['native_api_used']
                phase='explicit_empty'
                edge.set_enabled_edges([])
                assert native.read(hwnd)=={'vt':VT_EMPTY,'value':None},native.last_readback
                phase='reenabled_fullscreen'
                edge.set_enabled_edges(['left']); app.processEvents(); edge.native_policy.sync_now()
                require_true(hwnd)
                record=edge.native_policy._tracked[id(window)];cookie=record['cookie']
                phase='provider_forgot_window_then_close'
                close_observation={}
                assert observe_native_close(window,windows,edge.native_policy,close_observation)=={'vt':VT_EMPTY,'value':None},close_observation
                assert close_observation['cleanup_observed_before_destroy'] is True,close_observation
                assert not native.matches(hwnd,cookie),edge.native_policy.report()
                phase='reopen_after_close'
                windows.append(window); window.showFullScreen(); app.processEvents();edge.native_policy.sync_now()
                require_true(int(window.winId()))
                phase='edge_shutdown'
                edge.close()
                assert native.read(int(window.winId()))=={'vt':VT_EMPTY,'value':None},native.last_readback
                assert not edge.native_policy.report()['failures']
                assert not errors, errors
                print(json.dumps({'native':True,'set_get_empty_verified':True,'cookie_removed':True,'hardware':False,
                    'raw_readbacks':raw,'close_observation':close_observation}))
            except BaseException:
                print(json.dumps({'status':'failed','phase':phase,'policy':edge.native_policy.report(),
                    'last_readback':native.last_readback,'qt_errors':errors,'close_observation':locals().get('close_observation'),
                    'current_hwnd':int(window.winId()) if window.testAttribute(Qt.WidgetAttribute.WA_WState_Created) else None,
                    'original_hwnd':locals().get('hwnd'),
                    'original_hwnd_owned':native.is_own_window(locals().get('hwnd')),
                    'visible':window.isVisible(),'fullscreen':window.isFullScreen()}),flush=True)
                raise
            finally:
                edge.close();window.close();window.deleteLater();app.processEvents()
        ''')
        env = dict(os.environ, QT_QPA_PLATFORM='windows', PYTHONFAULTHANDLER='1')
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])['set_get_empty_verified'])


if __name__ == '__main__':
    unittest.main()
