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

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QImage
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
        assert len(lives) >= 3, 'Expected real H.264, HEVC, and JPEG pairs'
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

            viewer._video.videoSink().videoFrameChanged.connect(received)
            viewer.start_live()
            _wait(lambda: bool(valid_frames) and viewer._video.isVisible())
            assert abs(viewer.zoom_factor() - zoom) < .02, 'Video lost the zoomed viewport'
            viewer.stop_live()
            assert not viewer._video.isVisible() and viewer._photo.isVisible()
            viewer._video.videoSink().videoFrameChanged.disconnect(received)
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
        settings = SettingsDialog(window.preferences, window)
        assert not settings.live_key.keySequence().isEmpty()
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
