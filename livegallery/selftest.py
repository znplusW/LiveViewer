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
from PySide6.QtGui import QImage, QKeySequence, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from .cache import CacheStore
from .catalog_model import CatalogModel
from .imaging import load_image
from .library import scan_folder
from .matching import close_video_probe, resolve_pair, _video_samples
from .models import MediaItem
from .touch_grid import OverviewGrid
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


def _check_overview_touch_scroll(window, fixtures: Path):
    """Exercise the shipped overview with many rows without decoding more media."""
    grid = window.grid
    assert isinstance(grid, OverviewGrid)
    original_model = grid.model()
    scrolling_model = CatalogModel(grid)
    scrolling_model.set_items([MediaItem(f'touch-scroll-{row}', fixtures / 'ordinary.png')
                               for row in range(3000)])
    grid.setModel(scrolling_model)
    try:
        _wait(lambda: grid.verticalScrollBar().maximum() > 0, 3)
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(grid.viewport(), device, False)
        center = grid.viewport().rect().center()
        start = center + QPoint(0, 100)
        end = center - QPoint(0, 100)
        sequence.press(0, start, grid.viewport()).commit()
        sequence.move(0, center, grid.viewport()).commit()
        sequence.release(0, end, grid.viewport()).commit()
        QCoreApplication.processEvents()
        after_up = grid.verticalScrollBar().value()
        assert after_up > 0, 'Touch swipe did not scroll the overview'
        assert window._grid_mode and window.content.currentWidget() is grid, 'Swipe opened a photo'
        sequence.press(0, end, grid.viewport()).commit()
        sequence.move(0, center, grid.viewport()).commit()
        sequence.release(0, start, grid.viewport()).commit()
        QCoreApplication.processEvents()
        assert grid.verticalScrollBar().value() < after_up, 'Reverse touch swipe did not scroll back'
        assert window._grid_mode and window.content.currentWidget() is grid, 'Reverse swipe opened a photo'
    finally:
        grid.setModel(original_model)
        scrolling_model.deleteLater()


def _check_canvas_touch_and_wheel(viewer: ViewerCanvas, fixtures: Path):
    viewer.set_options(hold_ms=150, horizontal_scroll_mode='navigate')
    viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'), fixtures / 'Portrait_HEVC.MOV')
    navigation = []
    viewer.navigate_requested.connect(navigation.append)
    try:
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(viewer.viewport(), device, False)
        left = viewer.mapFromScene(QPointF(0, 0)).x()
        assert left > 160, 'Portrait fixture needs a wide blank letterbox'
        start = QPoint(left - 20, viewer.viewport().height() // 2)
        end = QPoint(20, start.y())
        assert not viewer.image_contains_viewport_point(QPointF(start))
        assert not viewer.image_contains_viewport_point(QPointF(end))
        for first, last in ((start, end), (end, start)):
            sequence.press(0, first, viewer.viewport()).commit()
            midpoint = QPoint((first.x() + last.x()) // 2, (first.y() + last.y()) // 2)
            sequence.move(0, midpoint, viewer.viewport()).commit()
            sequence.release(0, last, viewer.viewport()).commit()
            QCoreApplication.processEvents()
        assert navigation == [1, -1], f'Blank-area swipe direction mismatch: {navigation}'
        sequence.press(0, start, viewer.viewport()).commit()
        QTest.qWait(350)
        assert not viewer._play_requested and viewer._photo.isVisible(), 'Blank long press started video'
        sequence.release(0, start, viewer.viewport()).commit()
        QCoreApplication.processEvents()
        navigation.clear()

        center = QPointF(viewer.viewport().rect().center())

        def wheel(angle=QPoint(), pixels=QPoint()):
            event = QWheelEvent(center, QPointF(viewer.viewport().mapToGlobal(center.toPoint())),
                                pixels, angle, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(viewer.viewport(), event)
            QCoreApplication.processEvents()

        # Qt Windows reverses the native WM_MOUSEHWHEEL delta before delivery:
        # https://github.com/qt/qtbase/blob/v6.12.0/src/plugins/platforms/windows/qwindowspointerhandler.cpp
        for angle, pixels in ((QPoint(-120, 0), QPoint()), (QPoint(120, 0), QPoint()),
                              (QPoint(), QPoint(-60, 0)), (QPoint(), QPoint(60, 0))):
            QTest.qWait(220)
            wheel(angle, pixels)
        assert navigation == [1, -1, 1, -1], f'Horizontal wheel direction mismatch: {navigation}'
        while viewer.zoom_factor() < 3:
            viewer.zoom_in()
        QTest.qWait(220)
        wheel(angle=QPoint(-120, 0))
        assert navigation == [1, -1, 1, -1, 1], 'Default horizontal wheel changed mode when zoomed'
        viewer.set_options(horizontal_scroll_mode='pan')
        zoom, before = viewer.zoom_factor(), viewer._scene_center().x()
        wheel(pixels=QPoint(40, 0))
        assert viewer._scene_center().x() < before, 'Horizontal wheel did not pan the zoomed picture'
        assert len(navigation) == 5 and abs(viewer.zoom_factor() - zoom) < .005
        wheel(angle=QPoint(0, 120))
        assert viewer.zoom_factor() > zoom, 'Vertical wheel no longer zooms'
        viewer.set_options(horizontal_scroll_mode='pan')
        viewer.fit_image()
        wheel(angle=QPoint(-120, 0))
        assert len(navigation) == 5 and abs(viewer.zoom_factor() - 1) < .005
        viewer.set_options(horizontal_scroll_mode='navigate')
        return {'blank_swipe': [1, -1], 'horizontal_angle_and_pixel': [1, -1, 1, -1],
                'default_navigate_when_zoomed': True, 'blank_hold_starts_video': False,
                'zoomed_pan_and_vertical_zoom': True}
    finally:
        viewer.navigate_requested.disconnect(navigation.append)


def _check_windows_native_horizontal_wheel(viewer: ViewerCanvas):
    """Verify the Windows-message-to-Qt direction bridge on the real HWND."""
    import ctypes
    from .native_touch import _Point
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Point)]
    user32.ClientToScreen.restype = ctypes.c_int
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    viewer.activateWindow()
    viewer.raise_()
    viewer.setFocus()
    QCoreApplication.processEvents()
    window = viewer.window()
    hwnd = int(window.winId())
    local = viewer.viewport().mapTo(window, viewer.viewport().rect().center())
    ratio = window.devicePixelRatioF()
    point = _Point(round(local.x() * ratio), round(local.y() * ratio))
    assert user32.ClientToScreen(hwnd, ctypes.byref(point))
    packed = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
    navigation = []
    viewer.navigate_requested.connect(navigation.append)
    try:
        QTest.qWait(220)
        user32.SendMessageW(hwnd, 0x020E, 120 << 16, packed)  # WM_MOUSEHWHEEL: right
        _wait(lambda: bool(navigation), 3)
        assert navigation == [1], f'Windows right wheel did not advance once: {navigation}'
    finally:
        viewer.navigate_requested.disconnect(navigation.append)


def _check_windows_associated_apps(fixtures: Path):
    """Enumerate format-recommended handlers; never launch external apps."""
    from .system_open import (ASSOC_FILTER_RECOMMENDED, AssociatedApplication,
                              _ComPtr, _load_shell32, enumerate_apps)
    import ctypes
    assert ASSOC_FILTER_RECOMMENDED == 1

    class FormatEtc(ctypes.Structure):
        _fields_ = [('cfFormat', ctypes.c_uint16), ('ptd', ctypes.c_void_p),
                    ('dwAspect', ctypes.c_uint32), ('lindex', ctypes.c_int32),
                    ('tymed', ctypes.c_uint32)]

    class CaptureHandler:
        data_object_verified = False

        def call(self, slot, _argument_types, data_address):
            assert slot == 8 and data_address
            # The data object is borrowed here. AssociatedApplication.invoke
            # owns it and releases it after this callback; never close it here.
            data = _ComPtr(data_address)
            file_list = FormatEtc(15, None, 1, -1, 1)  # CF_HDROP / CONTENT / HGLOBAL
            result = data.call(5, [ctypes.POINTER(FormatEtc)], ctypes.byref(file_list))
            assert result == 0, f'Shell data object lacks a file list: 0x{result & 0xffffffff:08X}'
            self.data_object_verified = True
            return 0

        def close(self):
            pass

    captured = CaptureHandler()
    application = AssociatedApplication('Self-test capture', 'self-test-only', captured, _load_shell32())
    try:
        assert application.invoke(fixtures / 'ordinary.png') and captured.data_object_verified
    finally:
        application.close()

    result = {}
    for path in (fixtures / 'ordinary.png', fixtures / 'Live_H264.jpg'):
        for repetition in range(2):
            applications = enumerate_apps(path)
            try:
                assert all(isinstance(app.display_name, str) and app.display_name.strip()
                           and isinstance(app.identity, str) and app.identity.strip() for app in applications)
                assert len({app.identity.casefold() for app in applications}) == len(applications)
                assert all(app._handler.call(6, []) == 0 for app in applications), 'A handler is not recommended'
                if repetition == 0:
                    result[path.suffix] = [{'name': app.display_name, 'identity': app.identity}
                                          for app in applications]
            finally:
                for application in applications:
                    application.close()
                    application.close()  # Idempotence must not release COM twice.
    applications = enumerate_apps(fixtures / 'no-app.liveviewer-unassociated-74d96f1b')
    try:
        # A system can register a generic wildcard handler for unknown types.
        # Empty-list UI behavior is covered by unit tests; both native results
        # are valid here and must release every returned association object.
        assert all(app.display_name.strip() and app.identity.strip() for app in applications)
        unknown_count = len(applications)
    finally:
        for application in applications:
            application.close()
    return {'formats': result, 'unknown_extension_handlers': unknown_count,
            'association_filter': ASSOC_FILTER_RECOMMENDED}


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
        viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'))
        center = viewer.viewport().rect().center()
        for target in (2.0, 1.0):
            QTest.mouseDClick(viewer.viewport(), Qt.MouseButton.LeftButton, pos=center)
            assert viewer.is_zoom_animating(), 'Double-click did not start a zoom animation'
            viewer._zoom_animation.setCurrentTime(viewer._zoom_animation.duration() // 2)
            assert 1.01 < viewer.zoom_factor() < 1.99, 'Zoom animation has no intermediate scale'
            _wait(lambda: not viewer.is_zoom_animating(), 3)
            assert abs(viewer.zoom_factor() - target) < .005, 'Double-click missed its 1x/2x target'
        report['checks'].append({'check': 'double_click_animated_fit_to_two_times_zoom',
                                 'intermediate_scale': True, 'status': 'passed'})
        interactions = _check_canvas_touch_and_wheel(viewer, fixtures)
        report['checks'].append({'check': 'blank_letterbox_swipe_and_horizontal_wheel',
                                 **interactions, 'status': 'passed'})
        if sys.platform == 'win32' and app.platformName() == 'windows':
            viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'))
            QCoreApplication.processEvents()
            _check_windows_native_hold_policy(viewer)
            report['checks'].append({'check': 'windows_native_press_hold_image_vs_blank_policy',
                                     'real_hwnd': True, 'physical_touch_hardware': False,
                                     'status': 'passed'})
            _check_windows_native_horizontal_wheel(viewer)
            report['checks'].append({'check': 'windows_native_horizontal_wheel_direction',
                                     'native_delta': 120, 'navigation': 1,
                                     'physical_device_tested': False, 'status': 'passed'})
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
        window._toggle_fullscreen()
        QCoreApplication.processEvents()
        assert window._fullscreen_active and window._grid_mode
        assert window.content.currentWidget() is window.grid and window.grid.isVisible()
        assert not window.film.isVisible() and not window.fullscreen_live_badge.isVisible()
        assert not window.header.isVisible() and not window.filterbar.isVisible()
        _check_overview_touch_scroll(window, fixtures)
        window.grab().save(str(report_dir / 'fullscreen-overview.png'))
        QTest.keyClick(window.grid, Qt.Key.Key_Escape)
        assert not window._fullscreen_active and window._grid_mode
        report['checks'].append({'check': 'fullscreen_preserves_overview_and_touch_scroll',
                                 'virtual_rows': 3000, 'swipe_never_opens_photo': True,
                                 'status': 'passed'})
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
        assert window.fullscreen_live_badge.isVisible(), 'Fullscreen LIVE item has no overlay badge'
        badge_position = window.fullscreen_live_badge.pos()
        window.viewer.zoom_in()
        window.viewer._pan_by(QPointF(30, -20))
        QCoreApplication.processEvents()
        assert window.fullscreen_live_badge.pos() == badge_position, 'LIVE badge moved with the image'
        assert badge_position.x() < 50 and badge_position.y() < 50
        window.viewer.fit_image()
        chrome = (window.header, window.filterbar, window.infobar, window.viewer_tools,
                  window.sidebar, window.statusBar(), window.toast)
        assert not any(widget.isVisible() for widget in chrome), 'Fullscreen exposed a toolbar'
        QTest.mouseMove(window.viewer.viewport(), QPoint(30, 30))
        QTest.qWait(100)
        assert not any(widget.isVisible() for widget in chrome), 'Mouse movement restored fullscreen controls'
        QTest.keyClick(window.viewer, Qt.Key.Key_F)
        assert window.film.isVisible(), 'Filmstrip shortcut failed to reveal the filmstrip'
        QTest.keyClick(window.viewer, Qt.Key.Key_F)
        assert not window.film.isVisible(), 'Filmstrip shortcut failed to hide the filmstrip'
        QCoreApplication.processEvents()
        report['checks'].append({'check': 'filmstrip_shortcut_works_inside_fullscreen', 'status': 'passed'})
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
        assert not window.fullscreen_live_badge.isVisible()
        report['checks'].append({'check': 'immediate_clean_fullscreen_until_escape_or_f11',
                                 'filmstrip_optional': True, 'status': 'passed'})

        static_row = next(i for i, value in enumerate(window.model.items) if not value.is_live)
        static_path = window.model.items[static_row].image_path
        window._select_row(static_row)
        _wait(lambda: window._displayed_item is not None and window._displayed_item.image_path == static_path, 18)
        window._toggle_fullscreen()
        QCoreApplication.processEvents()
        assert not window.fullscreen_live_badge.isVisible(), 'Static photo incorrectly has a LIVE overlay'
        QTest.keyClick(window.viewer, Qt.Key.Key_Escape)
        assert not window._fullscreen_active and not window._grid_mode
        QTest.keyClick(window.viewer, Qt.Key.Key_Escape)
        assert window._grid_mode and window.content.currentWidget() is window.grid
        assert not window.fullscreen_live_badge.isVisible()
        report['checks'].append({'check': 'live_overlay_visibility_fixed_corner_and_escape_to_overview',
                                 'live_only': True, 'two_escape_sequence': True, 'status': 'passed'})

        if sys.platform == 'win32':
            import ctypes
            from .system_open import open_with
            assert callable(open_with) and ctypes.WinDLL('shell32').SHOpenWithDialog
            report['checks'].append({'check': 'windows_open_with_api_available',
                                     'dialog_launched': False, 'status': 'passed'})
            applications = _check_windows_associated_apps(fixtures)
            report['checks'].append({'check': 'windows_recommended_application_enumeration',
                                     'applications': applications['formats'], 'repeated_enumeration': True,
                                     'filter': 'ASSOC_FILTER_RECOMMENDED',
                                     'filter_value': applications['association_filter'],
                                     'real_shell_data_object_verified': True,
                                     'unknown_extension_handlers': applications['unknown_extension_handlers'],
                                     'apps_launched': False,
                                     'status': 'passed'})

        settings = SettingsDialog(window.preferences, window)
        assert not settings.live_key.keySequence().isEmpty()
        assert not settings.live_once_key.keySequence().isEmpty()
        assert not settings.filmstrip_key.keySequence().isEmpty()
        assert window.preferences.horizontal_scroll_mode == 'navigate'
        assert settings.horizontal_scroll_mode.currentIndex() == 0
        settings.horizontal_scroll_mode.setCurrentIndex(1)
        assert settings.horizontal_scroll_mode.currentIndex() == 1
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
