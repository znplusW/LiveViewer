"""Native-window settings input diagnostics, with an optional source context.

Run with QT_QPA_PLATFORM=windows:
python -m tests.settings_native_probe --report reports/native-settings-probe.json
Both child-widget and explicit QWindow touch routes are measured before the
unchanged production settings self-check. Failures retain actual event traces.
--source-context instead executes real source prerequisites with committed media,
skips the diagnostic HEIC encoding roundtrip, and stops after the settings check.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from livegallery.app import MainWindow, SettingsDialog
from livegallery.selftest import _check_settings_controls, _wait


def identity(widget):
    if widget is None:
        return None
    result = {'class': type(widget).__name__, 'object_name': widget.objectName()}
    text = getattr(widget, 'text', None)
    if callable(text):
        result['text'] = text()
    return result


class InputTrace(QObject):
    def __init__(self, control):
        super().__init__(control)
        self.control = control
        self.events = []
        self.phase = ''
        self.geometry = None

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd,
                            QEvent.Type.TouchCancel, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                            QEvent.Type.MouseButtonDblClick):
            row = {'phase': self.phase, 'receiver': identity(watched), 'type': event.type().name,
                   'checked': self.control.isChecked()}
            if hasattr(event, 'source'):
                row['source'] = event.source().name
            if hasattr(event, 'points'):
                row['points'] = [{'id': point.id(), 'state': point.state().name,
                                  'position': [point.position().x(), point.position().y()],
                                  'global': [point.globalPosition().x(), point.globalPosition().y()]}
                                 for point in event.points()]
            if event.type() == QEvent.Type.TouchBegin and self.geometry is not None:
                row['target_geometry'] = self.geometry()
            self.events.append(row)
        return False


class _SettingsContextComplete(BaseException):
    """Stop only outside run_self_test, after its existing finally has run."""


def source_context_probe(args, app):
    from livegallery import selftest
    report = {'status': 'failed', 'platform': app.platformName(), 'style': app.style().objectName(),
              'source_context': True, 'skipped_heic_in_diagnostic': True,
              'full_source_self_test': False, 'helper_reached': False,
              'synthesize_unhandled_touch': app.testAttribute(Qt.ApplicationAttribute.AA_SynthesizeMouseForUnhandledTouchEvents)}
    previous_data = os.environ.get('LIVEGALLERY_DATA_DIR')
    temporary = tempfile.TemporaryDirectory()
    os.environ['LIVEGALLERY_DATA_DIR'] = temporary.name
    settings = window = trace = None
    detailed = args.report.parent / (args.report.stem + '-source')
    detailed.mkdir(parents=True, exist_ok=True)
    original_helper = selftest._check_settings_controls

    def check_once(actual_settings, actual_window, report_dir):
        nonlocal settings, window, trace
        settings, window = actual_settings, actual_window
        report['helper_reached'] = True
        control = settings.autoplay
        position = QPoint(20, control.height() // 2)
        def geometry():
            global_point = control.mapToGlobal(position)
            handle = settings.windowHandle()
            return {'control': identity(control), 'control_size': [control.width(), control.height()],
                    'control_visible': control.isVisible(), 'control_enabled': control.isEnabled(),
                    'point_in_visible_region': control.visibleRegion().contains(position),
                    'point_in_scroll_viewport': settings.scroll_area.viewport().rect().contains(
                        control.mapTo(settings.scroll_area.viewport(), position)),
                    'device_pixel_ratio': control.devicePixelRatioF(),
                    'control_window_handle': control.windowHandle() is not None,
                    'settings_exposed': handle is not None and handle.isExposed(),
                    'settings_active': settings.isActiveWindow(),
                    'active_window': identity(app.activeWindow()),
                    'active_modal': identity(app.activeModalWidget()),
                    'global_point': [global_point.x(), global_point.y()],
                    'widget_at_point': identity(app.widgetAt(global_point)),
                    'widget_at_point_is_control': app.widgetAt(global_point) is control,
                    'accepts_touch': control.testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents),
                    'scroll_value': settings.scroll_area.verticalScrollBar().value()}
        trace = InputTrace(control)
        trace.phase = 'source-context-production-helper'
        trace.geometry = geometry
        app.installEventFilter(trace)
        report['geometry_before_helper'] = geometry()
        toggled, clicked = QSignalSpy(control.toggled), QSignalSpy(control.clicked)
        injections = []
        real_touch_event = QTest.touchEvent
        def record_injection(*arguments, **keywords):
            injections.append({'target': identity(arguments[0]), 'geometry': geometry()})
            return real_touch_event(*arguments, **keywords)
        # Read the existing report only; the preceding checks are not edited.
        frame = sys._getframe()
        while frame is not None and frame.f_code.co_name != 'run_self_test':
            frame = frame.f_back
        if frame is not None:
            report['preceding_source_checks'] = list(frame.f_locals['report']['checks'])
        try:
            with patch.object(QTest, 'touchEvent', side_effect=record_injection):
                report['settings_result'] = original_helper(settings, window, report_dir)
            report['status'] = 'passed'
        except Exception as failure:
            report.update(error=str(failure), traceback=traceback.format_exc())
        finally:
            report.update(toggles=toggled.count(), clicks=clicked.count(), checked=control.isChecked(),
                          geometry_after_helper=geometry(), touch_injections=injections,
                          events=list(trace.events))
            app.removeEventFilter(trace)
        if report['status'] != 'passed':
            # This comparison cannot turn a failed source-context check green.
            # app.run uses Fusion, whereas launch.py's self-test path currently
            # starts with the platform default style.
            app.setStyle('Fusion')
            trace.phase = 'source-context-fusion-retry'
            trace.events.clear()
            app.installEventFilter(trace)
            retry = {'diagnostic_only': True, 'style': app.style().objectName(),
                     'geometry_before_helper': geometry()}
            retry_injections = []
            retry_toggled, retry_clicked = QSignalSpy(control.toggled), QSignalSpy(control.clicked)
            def retry_injection(*arguments, **keywords):
                retry_injections.append({'target': identity(arguments[0]), 'geometry': geometry()})
                return real_touch_event(*arguments, **keywords)
            try:
                with patch.object(QTest, 'touchEvent', side_effect=retry_injection):
                    retry['settings_result'] = original_helper(settings, window, report_dir)
                retry['status'] = 'passed'
            except Exception as failure:
                retry.update(status='failed', error=str(failure), traceback=traceback.format_exc())
            finally:
                retry.update(toggles=retry_toggled.count(), clicks=retry_clicked.count(),
                             checked=control.isChecked(), geometry_after_helper=geometry(),
                             touch_injections=retry_injections, events=list(trace.events))
                app.removeEventFilter(trace)
            report['fusion_retry'] = retry
        raise _SettingsContextComplete()

    try:
        with patch.object(selftest, '_check_heic_cover_roundtrip',
                          return_value={'skipped_heic_in_diagnostic': True}), \
                patch.object(selftest, '_check_settings_controls', side_effect=check_once):
            try:
                result = selftest.run_self_test(Path(__file__).parent / 'fixtures', detailed)
            except _SettingsContextComplete:
                report['stopped_after_settings'] = True
            else:
                report.update(status='failed', source_return_code=result,
                              error='Source context did not stop after the settings helper')
                source_report = detailed / 'self-test.json'
                if source_report.is_file():
                    report['source_report'] = json.loads(source_report.read_text(encoding='utf-8'))
    except Exception as failure:
        report.update(status='failed', error=str(failure), traceback=traceback.format_exc())
    finally:
        if trace is not None:
            app.removeEventFilter(trace)
        if settings is not None:
            settings.reject()
        if window is not None:
            window.close()
            if not window.wait_for_shutdown(25):
                report.update(status='failed', shutdown_error=window._shutdown_error or 'shutdown timed out')
        try:
            temporary.cleanup()
        except OSError as failure:
            report.update(status='failed', cleanup_error=str(failure))
        if previous_data is None:
            os.environ.pop('LIVEGALLERY_DATA_DIR', None)
        else:
            os.environ['LIVEGALLERY_DATA_DIR'] = previous_data
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, default=Path('reports/native-settings-probe.json'))
    parser.add_argument('--expect-platform', default='windows' if sys.platform == 'win32' else '')
    parser.add_argument('--source-context', action='store_true',
                        help='Run real source preconditions through settings; skip only diagnostic HEIC roundtrip')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(['LiveViewer self-test'] if args.source_context else [])
    if args.source_context:
        if args.expect_platform and app.platformName() != args.expect_platform:
            raise RuntimeError('Unexpected Qt platform: ' + app.platformName())
        return source_context_probe(args, app)
    app.setStyle('Fusion')
    report = {'status': 'failed', 'platform': app.platformName(),
              'synthesize_unhandled_touch': app.testAttribute(Qt.ApplicationAttribute.AA_SynthesizeMouseForUnhandledTouchEvents),
              'checks': []}
    previous_data = os.environ.get('LIVEGALLERY_DATA_DIR')
    window = settings = trace = None
    temporary = None
    try:
        assert not args.expect_platform or app.platformName() == args.expect_platform
        temporary = tempfile.TemporaryDirectory()
        directory = temporary.name
        os.environ['LIVEGALLERY_DATA_DIR'] = directory
        window = MainWindow()
        window.show()
        settings = SettingsDialog(window.preferences, window)
        # Only this standalone probe needs desktop hit-testing of a child
        # without a native handle. Keep its test window unobscured even when
        # Windows refuses a background process's foreground activation request.
        settings.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        report['test_window_topmost'] = bool(settings.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        settings.show()
        settings.activateWindow()
        app.processEvents()
        control = settings.autoplay
        trace = InputTrace(control)
        app.installEventFilter(trace)
        position = QPoint(20, control.height() // 2)

        def geometry():
            global_point = control.mapToGlobal(position)
            handle = settings.windowHandle()
            return {'control_size': [control.width(), control.height()],
                    'control_visible': control.isVisible(), 'control_enabled': control.isEnabled(),
                    'point_in_visible_region': control.visibleRegion().contains(position),
                    'point_in_scroll_viewport': settings.scroll_area.viewport().rect().contains(
                        control.mapTo(settings.scroll_area.viewport(), position)),
                    'device_pixel_ratio': control.devicePixelRatioF(),
                    'control_window_handle': control.windowHandle() is not None,
                    'settings_exposed': handle is not None and handle.isExposed(),
                    'settings_active': settings.isActiveWindow(),
                    'global_window_is_settings': QGuiApplication.topLevelAt(global_point) is handle,
                    'active_window': identity(app.activeWindow()),
                    'active_modal': identity(app.activeModalWidget()),
                    'global_point': [global_point.x(), global_point.y()],
                    'widget_at_point': identity(app.widgetAt(global_point)),
                    'widget_at_point_is_control': app.widgetAt(global_point) is control,
                    'accepts_touch': control.testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents),
                    'scroll_value': settings.scroll_area.verticalScrollBar().value()}
        trace.geometry = geometry

        for route in ('child-widget', 'explicit-window'):
            trace.phase = route
            settings.scroll_area.ensureWidgetVisible(control)
            control.setFocus()
            app.processEvents()
            QTest.qWait(30)
            control.setChecked(False)
            toggled, clicked = QSignalSpy(control.toggled), QSignalSpy(control.clicked)
            result = {'route': route, 'geometry_before_ready': geometry(),
                      'target_ready': False, 'input_commits': 0}
            try:
                # A non-native child has no windowHandle(): QTest's QWidget
                # overload selects its QWindow through global hit-testing.
                # Qt active/exposed alone does not establish that precondition.
                # Make this test's window visible, then wait for the actual
                # target before sending one press/release pair (never retry it).
                settings.raise_()
                settings.activateWindow()
                def target_ready():
                    state = geometry()
                    result['geometry'] = state
                    return all(state[name] for name in (
                        'control_visible', 'control_enabled', 'point_in_visible_region',
                        'point_in_scroll_viewport', 'settings_exposed', 'settings_active',
                        'global_window_is_settings', 'widget_at_point_is_control'))
                try:
                    _wait(target_ready, seconds=1)
                except TimeoutError as failure:
                    raise AssertionError('Settings touch target is not globally visible/hittable') from failure
                result['target_ready'] = target_ready()
                assert result['target_ready'], 'Settings touch target is not globally visible/hittable'
                device = QTest.createTouchDevice()
                if route == 'child-widget':
                    target, point = control, position
                else:
                    target = settings.windowHandle()
                    point = control.mapTo(settings, position)
                sequence = QTest.touchEvent(target, device, False)
                sequence.press(0, point, target).commit()
                result['input_commits'] += 1
                sequence.release(0, point, target).commit()
                result['input_commits'] += 1
                result['checked_after_commit'] = control.isChecked()
                app.processEvents()
                QTest.qWait(80)
                result.update(checked=control.isChecked(), toggles=toggled.count(), clicks=clicked.count())
                assert control.isChecked() and toggled.count() == 1 and clicked.count() == 1
                result['status'] = 'passed'
            except Exception as failure:
                result.update(status='failed', error=str(failure), traceback=traceback.format_exc())
            report['checks'].append(result)

        trace.phase = 'production-helper'
        control.setChecked(False)
        toggled = QSignalSpy(control.toggled)
        result = {'route': 'production-helper', 'geometry_before': geometry()}
        injections = []
        real_touch_event = QTest.touchEvent
        def record_touch_injection(*arguments, **keywords):
            injections.append({'target': identity(arguments[0]), 'geometry': geometry()})
            return real_touch_event(*arguments, **keywords)
        try:
            with patch.object(QTest, 'touchEvent', side_effect=record_touch_injection):
                result.update(_check_settings_controls(settings, window, Path(directory)))
            result['status'] = 'passed'
        except Exception as failure:
            result.update(status='failed', error=str(failure), traceback=traceback.format_exc())
        result.update(toggles=toggled.count(), checked=control.isChecked(), geometry_after=geometry(),
                      touch_injections=injections)
        report['checks'].append(result)
        report['events'] = trace.events
        assert all(check['status'] == 'passed' for check in report['checks']), 'Native settings route failed; see checks/events'
        report['status'] = 'passed'
    except Exception as failure:
        report.update(error=str(failure), traceback=traceback.format_exc())
        if trace is not None:
            report['events'] = trace.events
    finally:
        if trace is not None:
            app.removeEventFilter(trace)
        if settings is not None:
            settings.reject()
        if window is not None:
            window.close()
            if not window.wait_for_shutdown(5):
                report.update(status='failed', shutdown_error=window._shutdown_error or 'shutdown timed out')
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError as failure:
                report.update(status='failed', cleanup_error=str(failure))
        if previous_data is None:
            os.environ.pop('LIVEGALLERY_DATA_DIR', None)
        else:
            os.environ['LIVEGALLERY_DATA_DIR'] = previous_data
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
