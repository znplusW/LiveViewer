"""Actual HWND edge-property diagnostics, without media or a full source run.

QT_QPA_PLATFORM=windows python -m tests.native_edge_probe \
    --report reports/native-edge-probe.json --expect-platform windows
The property and cookie are scoped to this process's temporary windows. This
does not verify physical edge gestures or modify any global Windows policy.
"""
from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget

from livegallery import native_fullscreen_gesture as api
from livegallery.fullscreen_edge_gesture import FullscreenEdgeGesture


def variant_snapshot(pointer):
    value = ctypes.cast(pointer, ctypes.POINTER(api.PROPVARIANT)).contents
    result = {'vt': int(value.vt), 'boolVal': int(value.boolVal), 'bytes': bytes(value).hex()}
    if value.vt == api.VT_LPWSTR:
        result['string'] = ctypes.wstring_at(value.pointer) if value.pointer else None
    return result


def apartment_snapshot():
    ole = ctypes.WinDLL('ole32.dll', winmode=0x800)
    get = ole.CoGetApartmentType
    get.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32)]
    get.restype = ctypes.c_int32
    kind, qualifier = ctypes.c_int32(), ctypes.c_int32()
    result = get(ctypes.byref(kind), ctypes.byref(qualifier))
    return {'hresult': f'0x{int(result) & 0xffffffff:08X}',
            'type': kind.value, 'qualifier': qualifier.value}


@contextmanager
def trace_property_calls(report, current):
    """Observe the real ABI calls, preserving store ownership and return values."""
    original = api._property_store

    @contextmanager
    def opened(hwnd):
        entry = {'stage': current[0], 'hwnd': int(hwnd), 'event': 'factory',
                 'apartment_before': apartment_snapshot()}
        report['com'].append(entry)
        try:
            with original(hwnd) as (store, ole):
                entry['store_pointer'] = store.address

                class TracedStore:
                    address = store.address

                    def call(self, slot, argument_types, *arguments):
                        row = {'stage': current[0], 'hwnd': int(hwnd),
                               'store_pointer': store.address, 'slot': slot}
                        report['com'].append(row)
                        if slot in (5, 6):
                            row['before'] = variant_snapshot(arguments[1])
                        try:
                            result = store.call(slot, argument_types, *arguments)
                            row['hresult'] = f'0x{int(result) & 0xffffffff:08X}'
                            if slot in (5, 6):
                                row['after'] = variant_snapshot(arguments[1])
                            return result
                        except Exception as error:
                            row['error'] = f'{type(error).__name__}: {error}'
                            raise

                yield TracedStore(), ole
            entry['released'] = True
            entry['apartment_after'] = apartment_snapshot()
        except Exception as error:
            entry['error'] = f'{type(error).__name__}: {error}'
            raise

    with patch.object(api, '_property_store', opened):
        yield


def run_probe(app, report):
    current = ['initializing']
    native = api.NativeWindowGestureStore()
    true, empty = {'vt': api.VT_BOOL, 'value': True}, {'vt': api.VT_EMPTY, 'value': None}

    def snapshot(window, policy=None):
        hwnd = int(window.winId()) if window.testAttribute(Qt.WidgetAttribute.WA_WState_Created) else 0
        record = policy._tracked.get(id(window)) if policy else None
        return {'hwnd': hwnd, 'own_window': native.is_own_window(hwnd),
                'cookie': native.user.GetPropW(hwnd, api.COOKIE_NAME) if hwnd else None,
                'visible': window.isVisible(), 'fullscreen': window.isFullScreen(),
                'active': window.isActiveWindow(),
                'record': {key: record.get(key) for key in ('hwnd', 'cookie', 'set')} if record else None,
                'policy': policy.report() if policy else None}

    def check(stage, window, function, expected, policy=None):
        current[0] = stage
        row = {'stage': stage, 'expected': expected, 'before': snapshot(window, policy)}
        report['checks'].append(row)
        try:
            row['actual'] = function()
            row['readback'] = dict(native.last_readback)
            assert row['actual'] == expected, f'{stage}: {row["actual"]!r} != {expected!r}'
            if expected['vt'] == api.VT_BOOL:
                assert row['readback']['raw_vt'] in (api.VT_BOOL, api.VT_LPWSTR), row
                assert row['readback']['conversion_succeeded'] is True, row
                assert row['readback']['canonical_vt'] == api.VT_BOOL and row['readback']['value'] is expected['value'], row
            else:
                assert row['readback']['raw_vt'] == api.VT_EMPTY and row['readback']['value'] is None, row
            row['status'] = 'passed'
        except Exception as error:
            row.update(status='failed', error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
        finally:
            row['readback'] = dict(native.last_readback)
            row['after'] = snapshot(window, policy)
        return row

    with trace_property_calls(report, current):
        direct = QWidget()
        direct.setObjectName('native-edge-probe-direct')
        try:
            direct.showFullScreen()
            app.processEvents()
            hwnd = int(direct.winId())

            def set_with_store(store, ole):
                value = api.PROPVARIANT()
                value.vt, value.boolVal = api.VT_BOOL, api.VARIANT_TRUE
                api._hresult(store.call(6, [ctypes.POINTER(api.PROPERTYKEY), ctypes.POINTER(api.PROPVARIANT)],
                                       ctypes.byref(api.EDGE_PROPERTY), ctypes.byref(value)), 'Probe SetValue failed')
                native.last_readback = {}
                return native._read(store, ole, native.last_readback)

            def read_with_store(store, ole):
                native.last_readback = {}
                return native._read(store, ole, native.last_readback)

            def same_store():
                with api._property_store(hwnd) as (store, ole):
                    return set_with_store(store, ole)

            check('direct_same_store_set_get', direct, same_store, true)
            check('direct_released_then_fresh_get', direct, lambda: native.read(hwnd), true)
            check('direct_native_write', direct, lambda: native.write(hwnd, True), true)
            check('direct_native_fresh_read', direct, lambda: native.read(hwnd), true)
            with api._property_store(hwnd) as (held, ole):
                check('direct_held_store_set_get', direct, lambda: set_with_store(held, ole), true)
                check('direct_held_store_read', direct, lambda: read_with_store(held, ole), true)
                check('direct_fresh_while_other_held', direct, lambda: native.read(hwnd), true)

            ole = ctypes.WinDLL('ole32.dll', winmode=0x800)
            ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            ole.CoInitializeEx.restype = ctypes.c_int32
            ole.CoUninitialize.argtypes, ole.CoUninitialize.restype = [], None
            initialized = int(ole.CoInitializeEx(None, 2)) & 0xffffffff
            report['external_apartment'] = {'initialize_hresult': f'0x{initialized:08X}',
                                           'before_factory': apartment_snapshot()}
            try:
                if initialized != 0x80010106:
                    api._hresult(initialized, 'Probe apartment hold failed')
                check('held_apartment_native_write', direct, lambda: native.write(hwnd, True), true)
                check('held_apartment_fresh_read', direct, lambda: native.read(hwnd), true)
                report['external_apartment']['after_factory'] = apartment_snapshot()
            finally:
                if initialized in (0, 1):
                    ole.CoUninitialize()
                report['external_apartment']['after_uninitialize'] = apartment_snapshot()
            check('released_apartment_fresh_read', direct, lambda: native.read(hwnd), true)
            check('direct_empty_write', direct, lambda: native.write(hwnd, None), empty)
            check('direct_empty_fresh_get', direct, lambda: native.read(hwnd), empty)
        finally:
            current[0] = 'direct_finally_cleanup'
            try:
                native.write(int(direct.winId()), None)
            except Exception as error:
                report['cleanup_errors'].append(f'{type(error).__name__}: {error}')
            direct.close()
            direct.deleteLater()
            app.processEvents()

        simple = QWidget()
        simple.setObjectName('native-edge-probe-policy')
        windows = [simple]
        edge = FullscreenEdgeGesture(lambda: windows)
        try:
            simple.showFullScreen()
            app.processEvents()
            edge.native_policy.sync_now()
            policy = edge.native_policy
            hwnd = int(simple.winId())
            check('policy_fullscreen_true', simple, lambda: native.read(hwnd), true, policy)
            edge.set_enabled_edges([])
            check('policy_explicit_empty', simple, lambda: native.read(hwnd), empty, policy)
            edge.set_enabled_edges(['left'])
            app.processEvents()
            policy.sync_now()
            check('policy_reenabled_true', simple, lambda: native.read(hwnd), true, policy)
            simple.showNormal()
            app.processEvents()
            policy.sync_now()
            check('policy_exit_empty', simple, lambda: native.read(hwnd), empty, policy)
            simple.showFullScreen()
            app.processEvents()
            policy.sync_now()
            hwnd = int(simple.winId())
            check('policy_before_forgot_provider_true', simple, lambda: native.read(hwnd), true, policy)
            record = policy._tracked.get(id(simple))
            cookie = record['cookie'] if record else None
            windows.clear()
            simple.close()
            app.processEvents()
            check('policy_forgot_provider_close_empty', simple, lambda: native.read(hwnd), empty, policy)
            report['forgot_provider_close'] = {'original_hwnd': hwnd,
                'original_hwnd_owned': native.is_own_window(hwnd),
                'cookie_matches': native.matches(hwnd, cookie) if cookie else None,
                'policy': policy.report()}
            windows.append(simple)
            simple.showFullScreen()
            app.processEvents()
            policy.sync_now()
            check('policy_reopen_after_close_true', simple, lambda: native.read(int(simple.winId())), true, policy)
            edge.close()
            check('policy_shutdown_empty', simple, lambda: native.read(int(simple.winId())), empty, policy)
            report['standalone_policy'] = policy.report()
        finally:
            report['standalone_policy_before_cleanup'] = edge.native_policy.report()
            edge.close()
            simple.close()
            simple.deleteLater()
            app.processEvents()

        # Use the actual application window, but no library, media, or decoding.
        with tempfile.TemporaryDirectory(prefix='liveviewer-native-edge-probe-') as temporary, \
                patch.dict(os.environ, LIVEGALLERY_DATA_DIR=str(Path(temporary) / 'data')):
            from livegallery.app import MainWindow
            main = MainWindow()
            try:
                main.show()
                app.processEvents()
                main._toggle_fullscreen()
                app.processEvents()
                policy = main.fullscreen_edge_gesture.native_policy
                policy.sync_now()
                hwnd = int(main.winId())
                check('main_fullscreen_true', main, lambda: native.read(hwnd), true, policy)
                main._exit_fullscreen()
                app.processEvents()
                policy.sync_now()
                check('main_exit_empty', main, lambda: native.read(hwnd), empty, policy)
                report['main_policy'] = policy.report()
            finally:
                report['main_policy_before_cleanup'] = main.fullscreen_edge_gesture.native_policy.report()
                main.close()
                assert main.wait_for_shutdown(25), main._shutdown_error
                main.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--expect-platform', default='windows')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    report = {'status': 'failed', 'platform': app.platformName(), 'os': sys.platform,
              'checks': [], 'com': [], 'cleanup_errors': [], 'qt_errors': [],
              'physical_touch_delivery_verified': False, 'global_policy_changed': False}
    previous = sys.excepthook
    sys.excepthook = lambda kind, value, tb: report['qt_errors'].append(''.join(traceback.format_exception(kind, value, tb)))
    try:
        assert app.platformName() == args.expect_platform, report['platform']
        assert sys.platform == 'win32', 'Actual HWND probe requires Windows'
        run_probe(app, report)
        assert report['checks'] and all(row['status'] == 'passed' for row in report['checks']), 'Native property readback failed'
        assert not report['cleanup_errors'] and not report['qt_errors'], 'Native/Qt cleanup failed'
        assert not report['standalone_policy']['failures'] and not report['main_policy']['failures'], 'Native policy reported failure'
        report['status'] = 'passed'
    except Exception as error:
        report.update(error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
    finally:
        sys.excepthook = previous
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'platform': report['platform'],
                      'checks': [{key: row.get(key) for key in ('stage', 'status', 'actual', 'error')}
                                 for row in report['checks']], 'error': report.get('error')}), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
