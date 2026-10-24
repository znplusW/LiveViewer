"""Exercise real decoders and the shipped application's media path offline."""
from __future__ import annotations

import json
import os
import platform
import struct
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from .cache import CacheStore
from .imaging import load_image
from .library import scan_folder
from .matching import close_video_probe, resolve_pair, _video_samples
from .viewer import ViewerCanvas


def _wait(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError('Media event did not complete within its time limit')


def _qimage(path):
    image = load_image(path, 1600).convert('RGBA')
    return QImage(image.tobytes(), image.width, image.height, QImage.Format.Format_RGBA8888).copy()


def _assert_portrait_corners(image: QImage):
    assert image.height() > image.width(), 'Display rotation did not produce a portrait frame'
    corners = [image.pixelColor(round(image.width() * x), round(image.height() * y))
               for x, y in ((.04, .04), (.96, .04), (.04, .96), (.96, .96))]
    green, yellow, red, blue = [(c.red(), c.green(), c.blue()) for c in corners]
    assert green[1] > max(green[0], green[2]) + 40, f'Portrait top left: {green}'
    assert min(yellow[0], yellow[1]) > yellow[2] + 60, f'Portrait top right: {yellow}'
    assert red[0] > max(red[1], red[2]) + 40, f'Portrait bottom left: {red}'
    assert blue[2] > max(blue[0], blue[1]) + 40, f'Portrait bottom right: {blue}'


def _check_windows_native_hold_policy(viewer: ViewerCanvas):
    """Query the real HWND; this verifies policy routing, not touch hardware."""
    import ctypes
    from .native_touch import (TABLET_DISABLE_PRESSANDHOLD,
                               WM_TABLET_QUERYSYSTEMGESTURESTATUS, _Point)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Point)]
    user32.ClientToScreen.restype = ctypes.c_int
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    window = viewer.window()
    hwnd = int(window.winId())
    image_center = viewer.mapFromScene(viewer._image_rect.center())
    blank_corner = QPoint(2, 2)
    assert viewer.image_contains_viewport_point(QPointF(image_center))
    assert not viewer.image_contains_viewport_point(QPointF(blank_corner)), 'Fixture needs a letterbox'

    def query(position):
        local = viewer.viewport().mapTo(window, position)
        ratio = window.devicePixelRatioF()
        point = _Point(round(local.x() * ratio), round(local.y() * ratio))
        assert user32.ClientToScreen(hwnd, ctypes.byref(point))
        packed = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
        return user32.SendMessageW(hwnd, WM_TABLET_QUERYSYSTEMGESTURESTATUS, 0, packed)

    assert query(image_center) & TABLET_DISABLE_PRESSANDHOLD, 'Image permits Windows press-and-hold'
    assert not query(blank_corner) & TABLET_DISABLE_PRESSANDHOLD, 'Blank area blocks Windows press-and-hold'


def run_self_test(fixtures: Path | None, report_dir: Path) -> int:
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        'status': 'failed',
        'environment': {
            'os': platform.system(), 'machine': platform.machine(),
            'pointer_bits': struct.calcsize('P') * 8,
            'frozen': bool(getattr(sys, 'frozen', False)),
            'python': platform.python_version(),
        },
        'checks': [],
    }
    cache = None
    viewer = None
    window = None
    app = QApplication.instance() or QApplication(['LiveViewer self-test'])
    report['environment']['qt_platform'] = app.platformName()
    try:
        if sys.platform == 'win32':
            assert report['environment']['pointer_bits'] == 64
            assert platform.machine().casefold() in {'amd64', 'x86_64'}
        assert fixtures is not None and fixtures.is_dir(), 'Media fixtures directory is required'
        fixtures = fixtures.resolve()
        expected = {'.jpg', '.jpeg', '.heic', '.png', '.webp', '.bmp', '.gif', '.tiff', '.avif'}
        seen = set()
        for path in sorted(fixtures.iterdir()):
            if path.suffix.casefold() in expected:
                image = load_image(path, 512)
                assert image.width > 0 and image.height > 0
                seen.add(path.suffix.casefold())
                report['checks'].append({'check': 'image_decoder', 'file': path.name,
                                         'size': list(image.size), 'status': 'passed'})
        assert expected <= seen, f'Missing decoder fixtures: {expected - seen}'
        cache = CacheStore(report_dir / 'test-cache', 8 * 1024 ** 2)
        catalog = scan_folder(fixtures)
        lives = [item for item in catalog if item.is_live]
        assert len(lives) >= 4, 'Expected H.264, HEVC, JPEG and rotated portrait pairs'
        assert any(item.image_path.suffix.casefold() == '.jpeg' for item in lives)
        for item in catalog:
            thumbnail = cache.get_thumbnail(item.image_path)
            assert thumbnail.is_file()
        report['checks'].append({'check': 'index_and_thumbnails', 'status': 'passed',
                                 'items': len(catalog), 'live_items': len(lives)})

        for item in lives:
            decision = resolve_pair(item, cache)
            assert decision.image_path.is_file()
            if item.image_path.stem == 'Live_H264':
                assert decision.image_path.suffix.casefold() == '.heic', 'Equivalent HEIC should win'
            if item.image_path.stem == 'Live_HEVC':
                assert decision.image_path.suffix.casefold() == '.jpg', 'Matching still should win'
                assert any(p.suffix.casefold() == '.jpeg' for p in decision.extra_images)
            report['checks'].append({'check': 'automatic_image_choice', 'file': item.video_path.name,
                                     'chosen': decision.image_path.name, 'uncertain': decision.uncertain,
                                     'reason': decision.reason, 'status': 'passed'})
        # Exercise the persistent child process even when all candidates happen
        # to be visually equivalent, including the windowed Windows executable.
        for item in lives[:2]:
            frames, _ = _video_samples(item.video_path)
            assert len(frames) >= 2, f'Isolated media probe failed: {item.video_path.name}'
            report['checks'].append({'check': 'isolated_video_probe', 'file': item.video_path.name,
                                     'frames': len(frames), 'status': 'passed'})

        viewer = ViewerCanvas()
        viewer.resize(960, 640)
        viewer.show()
        QCoreApplication.processEvents()
        for item in lives:
            viewer.set_options(loop=True, muted=True, preserve_zoom=True)
            viewer.set_photo(_qimage(item.image_path), item.video_path)
            viewer.zoom_in()
            zoom = viewer.zoom_factor()
            valid_frames = []

            def received(frame):
                if frame.isValid() and len(valid_frames) < 2:
                    converted = frame.toImage()
                    if not converted.isNull():
                        valid_frames.append(converted.size())

            viewer._video_sink.videoFrameChanged.connect(received)
            viewer.start_live()
            _wait(lambda: bool(valid_frames) and viewer._video.isVisible())
            assert abs(viewer.zoom_factor() - zoom) < .02, 'Video lost the zoomed viewport'
            rendered = viewer._video.pixmap().toImage()
            assert not rendered.isNull(), 'Playback did not present a complete image frame'
            if item.image_path.stem == 'Portrait_HEVC':
                _assert_portrait_corners(rendered)
                rendered.save(str(report_dir / 'rotated-portrait-frame.png'))
                report['checks'].append({'check': 'rotated_hevc_portrait_orientation',
                                         'rendered_frame': [rendered.width(), rendered.height()],
                                         'asymmetric_corners': 'passed', 'status': 'passed'})
            viewer.stop_live()
            assert not viewer._video.isVisible() and viewer._photo.isVisible()
            viewer._video_sink.videoFrameChanged.disconnect(received)
            report['checks'].append({'check': 'actual_video_playback_and_release',
                                     'file': item.video_path.name, 'zoom_preserved': True,
                                     'decoded_frame': [valid_frames[0].width(), valid_frames[0].height()],
                                     'status': 'passed'})

        item = lives[0]
        viewer.set_options(loop=True, muted=True, preserve_zoom=False)
        viewer.set_photo(_qimage(item.image_path), item.video_path)
        viewer.zoom_in()
        viewer.zoom_in()
        zoom = viewer.zoom_factor()
        viewer.start_live()
        _wait(lambda: viewer._video.isVisible())
        assert abs(viewer.zoom_factor() - 1) < .02
        viewer.stop_live()
        assert abs(viewer.zoom_factor() - zoom) < .02
        report['checks'].append({'check': 'temporary_fit_restores_original_zoom', 'status': 'passed'})
        viewer.set_options(loop=False)
        viewer.start_live()
        _wait(lambda: viewer._video.isVisible())
        _wait(lambda: not viewer._video.isVisible(), 8)
        report['checks'].append({'check': 'single_play_returns_to_photo', 'status': 'passed'})
        viewer.set_options(loop=True)
        viewer.play_live_once()
        _wait(lambda: viewer._video.isVisible())
        viewer.release_live()
        assert viewer._video.isVisible(), 'Releasing a held input cancelled single-click playback'
        _wait(lambda: not viewer._video.isVisible(), 8)
        assert viewer._photo.isVisible()
        report['checks'].append({'check': 'single_click_ignores_hold_loop_and_release', 'status': 'passed'})
        if sys.platform == 'win32' and app.platformName() == 'windows':
            viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'))
            QCoreApplication.processEvents()
            _check_windows_native_hold_policy(viewer)
            report['checks'].append({'check': 'windows_native_press_hold_image_vs_blank_policy',
                                     'real_hwnd': True, 'physical_touch_hardware': False,
                                     'status': 'passed'})
        viewer.grab().save(str(report_dir / 'canvas.png'))
        viewer.close()
        # The release must exercise the real main window as well as the media
        # canvas, so missing UI imports or wiring cannot hide behind a CLI pass.
        os.environ['LIVEGALLERY_DATA_DIR'] = str(report_dir / 'ui-data')
        from .app import MainWindow, SettingsDialog
        window = MainWindow()
        window.resize(1280, 850)
        window.show()
        window.load_folder(fixtures)
        _wait(lambda: window._displayed_item is not None and not window.viewer._photo.pixmap().isNull(), 18)
        assert len(window.model.items) >= 9
        first_id = window._current_id
        window._navigate(1)
        _wait(lambda: window._current_id != first_id and window._displayed_item is not None, 18)
        window.grid_button.setChecked(True)
        window._toggle_grid(True)
        QCoreApplication.processEvents()
        assert window.content.currentWidget() is window.grid
        window.grab().save(str(report_dir / 'main-window-grid.png'))
        window.grid_button.setChecked(False)
        window._toggle_grid(False)
        window.grab().save(str(report_dir / 'main-window.png'))

        # Exercise the real filter widgets rather than only filtering a list in
        # isolation: both types selected, either type alone, and neither type.
        window.kind_filter.static.setChecked(False)
        assert window.model.items and all(item.is_live for item in window.model.items)
        window.kind_filter.live.setChecked(False)
        assert not window.model.items
        window.kind_filter.static.setChecked(True)
        assert window.model.items and all(not item.is_live for item in window.model.items)
        window.kind_filter.live.setChecked(True)
        assert any(item.is_live for item in window.model.items)
        assert any(not item.is_live for item in window.model.items)
        report['checks'].append({'check': 'overview_multi_select_type_filter', 'status': 'passed'})

        live_row = next(i for i, value in enumerate(window.model.items)
                        if value.is_live and value.image_path.stem == 'JPEG_PAIR')
        window._select_row(live_row)
        _wait(lambda: window._displayed_item is not None
              and window._displayed_item.image_path.stem == 'JPEG_PAIR', 18)
        window.activateWindow()
        window.viewer.setFocus()
        QTest.qWait(50)
        window.viewer.set_options(loop=True)
        QTest.keyPress(window.viewer, Qt.Key.Key_P)
        _wait(lambda: window.viewer._video.isVisible())
        QTest.keyRelease(window.viewer, Qt.Key.Key_P)
        assert window.viewer._video.isVisible(), 'P key release stopped the video'
        _wait(lambda: not window.viewer._video.isVisible(), 8)
        report['checks'].append({'check': 'main_window_single_click_playback_shortcut', 'status': 'passed'})

        window._toggle_strip(False)
        window._toggle_fullscreen()
        QCoreApplication.processEvents()
        assert window._fullscreen_active and not window.film.isVisible()
        chrome = (window.header, window.filterbar, window.infobar, window.viewer_tools,
                  window.sidebar, window.statusBar(), window.toast)
        assert not any(widget.isVisible() for widget in chrome), 'Fullscreen exposed a toolbar'
        QTest.mouseMove(window.viewer.viewport(), QPoint(30, 30))
        QTest.qWait(100)
        assert not any(widget.isVisible() for widget in chrome), 'Mouse movement restored fullscreen controls'
        window.grab().save(str(report_dir / 'fullscreen-photo-only.png'))
        QTest.keyClick(window.viewer, Qt.Key.Key_F11)
        assert not window._fullscreen_active and window.header.isVisible(), 'F11 did not exit fullscreen'
        QTest.keyClick(window.viewer, Qt.Key.Key_F11)
        assert window._fullscreen_active and not any(widget.isVisible() for widget in chrome)
        QTest.keyClick(window.viewer, Qt.Key.Key_Escape)
        assert not window._fullscreen_active
        window._toggle_strip(True)
        window._toggle_fullscreen()
        QCoreApplication.processEvents()
        assert window.film.isVisible() and window.strip.isVisible()
        assert not window.library_label.isVisible() and not window.analysis_label.isVisible()
        assert not any(widget.isVisible() for widget in chrome)
        window.grab().save(str(report_dir / 'fullscreen-with-filmstrip.png'))
        QTest.keyClick(window.viewer, Qt.Key.Key_Escape)
        assert not window._fullscreen_active and window.header.isVisible()
        report['checks'].append({'check': 'immediate_clean_fullscreen_until_escape_or_f11',
                                 'filmstrip_optional': True, 'status': 'passed'})

        settings = SettingsDialog(window.preferences, window)
        assert not settings.live_key.keySequence().isEmpty()
        assert not settings.live_once_key.keySequence().isEmpty()
        settings.live_once_key.setKeySequence(QKeySequence('F9'))
        assert settings.live_once_key.keySequence().toString() == 'F9'
        settings.show()
        QCoreApplication.processEvents()
        settings.grab().save(str(report_dir / 'settings.png'))
        settings.close()
        report['checks'].append({'check': 'main_window_scan_navigation_grid_and_settings', 'status': 'passed'})
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        report['traceback'] = traceback.format_exc()
    finally:
        if window:
            window.close()
        if viewer:
            viewer.close()
        close_video_probe()
        if cache:
            cache.close()
        QCoreApplication.processEvents()
    (report_dir / 'self-test.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if sys.stdout is not None:
        print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == 'passed' else 1
