"""Fast native-window settings input probe, without decoding or HEIC builds.

Run with QT_QPA_PLATFORM=windows:
python -m tests.settings_native_probe --report reports/native-settings-probe.json
Both child-widget and explicit QWindow touch routes are measured before the
unchanged production settings self-check. Failures retain actual event traces.
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
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from livegallery.app import MainWindow, SettingsDialog
from livegallery.selftest import _check_settings_controls


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
                            QEvent.Type.TouchCancel, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, default=Path('reports/native-settings-probe.json'))
    parser.add_argument('--expect-platform', default='windows' if sys.platform == 'win32' else '')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
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
            result = {'route': route, 'geometry': geometry()}
            try:
                device = QTest.createTouchDevice()
                if route == 'child-widget':
                    target, point = control, position
                else:
                    target = settings.windowHandle()
                    point = control.mapTo(settings, position)
                sequence = QTest.touchEvent(target, device, False)
                sequence.press(0, point, target).commit()
                sequence.release(0, point, target).commit()
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
