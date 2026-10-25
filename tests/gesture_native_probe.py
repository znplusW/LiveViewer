"""Compare exact input timestamps with the actual native cursor aligned or away.

Run on a native Qt platform, optionally after the preceding canvas/navigation
self-checks. This records the unchanged assertions in the real gesture helper;
an observed baseline failure is diagnostic, never permission to weaken a gate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from livegallery.selftest import (_check_canvas_touch_and_wheel,
                                 _check_zoom_navigation_overview,
                                 _check_zoomed_double_tap_drag, _wait)
from livegallery.viewer import ViewerCanvas


def run_case(app, fixtures, report_dir, *, align_cursor, source_context):
    report = {'aligned': align_cursor, 'status': 'failed', 'source_context': source_context}
    neutral = QWidget()
    neutral.setObjectName('gesture-probe-neutral-cursor-target')
    neutral.setGeometry(20, 20, 90, 90)
    viewer = ViewerCanvas()
    viewer.setObjectName('gesture-probe-viewer')
    viewer.setGeometry(260, 180, 960, 640)
    try:
        neutral.show()
        viewer.show()
        _wait(lambda: viewer.windowHandle() is not None and viewer.windowHandle().isExposed(), 5)
        viewer.activateWindow()
        viewer.setFocus()
        QCoreApplication.processEvents()
        if source_context:
            report['canvas'] = _check_canvas_touch_and_wheel(viewer, fixtures)
            report['navigator'] = _check_zoom_navigation_overview(viewer, fixtures, report_dir)
        # A known real cursor location distinguishes event globalPosition from
        # the OS cursor, without adding a click or changing gesture timing.
        neutral.raise_()
        QTest.mouseMove(neutral, neutral.rect().center())
        viewer.activateWindow()
        viewer.setFocus()
        QCoreApplication.processEvents()
        actual = QCursor.pos()
        report['before'] = {'cursor_global': [actual.x(), actual.y()],
                            'cursor_inside_viewer': viewer.viewport().rect().contains(
                                viewer.viewport().mapFromGlobal(actual)),
                            'active': viewer.isActiveWindow(),
                            'exposed': viewer.windowHandle().isExposed()}
        report['trace'] = {}
        report['result'] = _check_zoomed_double_tap_drag(viewer, fixtures,
                                                       diagnostics=report['trace'],
                                                       align_cursor=align_cursor)
        report['status'] = 'passed'
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc())
    finally:
        viewer.stop_live()
        viewer.close()
        neutral.close()
        QCoreApplication.processEvents()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--source-context', action='store_true')
    parser.add_argument('--expect-platform')
    parser.add_argument('--align-cursor', action='store_true',
                        help='Run only the aligned case, rather than the baseline/aligned pair.')
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    original = QCursor.pos()
    report = {'platform': app.platformName(), 'style': app.style().objectName(),
              'status': 'failed', 'physical_touchpad_hardware': False, 'cases': []}
    try:
        if args.expect_platform and app.platformName() != args.expect_platform:
            raise AssertionError(f'Expected {args.expect_platform}, got {app.platformName()}')
        fixtures = Path(__file__).resolve().parent / 'fixtures'
        for aligned in ((True,) if args.align_cursor else (False, True)):
            report['cases'].append(run_case(app, fixtures, args.report.parent,
                                            align_cursor=aligned, source_context=args.source_context))
        report['baseline_reproduced'] = any(not case['aligned'] and case['status'] == 'failed'
                                             for case in report['cases'])
        report['status'] = 'passed' if report['cases'][-1]['status'] == 'passed' else 'failed'
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc())
    finally:
        QCursor.setPos(original)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'platform': report['platform'],
                      'baseline_reproduced': report.get('baseline_reproduced'),
                      'cases': [{'aligned': case['aligned'], 'status': case['status'],
                                 'error': case.get('error')} for case in report['cases']]}), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
