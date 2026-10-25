"""Exercise real decoders and the shipped application's media path offline."""
from __future__ import annotations

import json
import os
import platform
import hashlib
import shutil
import struct
import subprocess
import sys
import time
import traceback
import tempfile
from pathlib import Path
from contextlib import contextmanager

from PySide6.QtCore import QCoreApplication, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from . import __version__
from .cache import CacheStore
from .catalog_model import CatalogModel
from .imaging import load_image
from .library import scan_folder
from .matching import close_video_probe, resolve_pair, _video_samples
from .models import MediaItem
from .touch_grid import OverviewGrid
from .viewer import ViewerCanvas
from .frame_reader import FrameReader


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


def _check_overview_touch_scroll(window, fixtures: Path, report_dir: Path):
    """Exercise many rows using a bounded set of synthetic fixture thumbnails."""
    grid = window.grid
    assert isinstance(grid, OverviewGrid)
    original_model = grid.model()
    original_width = grid.thumbnail_width
    original_id = window._current_id
    scrolling_model = CatalogModel(grid)
    paths = [fixtures / name for name in ('ordinary.png', 'Portrait_HEVC.jpg', 'Live_H264.jpg')]
    scrolling_model.set_items([MediaItem(f'touch-scroll-{row}', paths[row % len(paths)])
                               for row in range(3000)])
    for path in paths:
        scrolling_model.put_thumbnail(path, _qimage(path))
    clicked = []
    def record_click(index):
        clicked.append(index.row())
    grid.clicked.connect(record_click)
    grid.setModel(scrolling_model)
    try:
        _wait(lambda: grid.verticalScrollBar().maximum() > 0, 3)
        device = QTest.createTouchDevice()
        def columns():
            first = grid.visualRect(scrolling_model.index(0))
            return sum(grid.visualRect(scrolling_model.index(row)).top() == first.top()
                       for row in range(40))
        def pinch(start_half, end_half):
            center = grid.viewport().rect().center()
            gesture = QTest.touchEvent(grid.viewport(), device, False)
            left, right = center - QPoint(start_half, 0), center + QPoint(start_half, 0)
            gesture.press(0, left, grid.viewport()).press(1, right, grid.viewport()).commit()
            gesture.move(0, center - QPoint(end_half, 0), grid.viewport()).move(
                1, center + QPoint(end_half, 0), grid.viewport()).commit()
            QCoreApplication.processEvents()
            gesture.release(0, center - QPoint(end_half, 0), grid.viewport()).release(
                1, center + QPoint(end_half, 0), grid.viewport()).commit()
            QCoreApplication.processEvents()

        grid.verticalScrollBar().setValue(0)
        QCoreApplication.processEvents()
        before_columns = columns()
        before_width = grid.thumbnail_width
        window.grab().save(str(report_dir / 'overview-zoom-before.png'))
        small_half = min(50, grid.viewport().width() // 10)
        large_half = min(180, grid.viewport().width() // 3)
        pinch(small_half, large_half)
        _wait(lambda: grid.thumbnail_width > before_width and columns() < before_columns, 3)
        large_width, large_columns = grid.thumbnail_width, columns()
        grid.verticalScrollBar().setValue(0)
        QCoreApplication.processEvents()
        window.grab().save(str(report_dir / 'overview-zoom-larger.png'))
        pinch(large_half, small_half)
        _wait(lambda: grid.thumbnail_width < large_width and columns() > large_columns, 3)
        small_width, small_columns = grid.thumbnail_width, columns()
        grid.verticalScrollBar().setValue(0)
        QCoreApplication.processEvents()
        window.grab().save(str(report_dir / 'overview-zoom-smaller.png'))

        grid.setFocus()
        QTest.keyClick(grid, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
        assert grid.thumbnail_width > small_width, 'Ctrl+plus did not enlarge overview thumbnails'
        for _ in range(16):
            if columns() < small_columns:
                break
            QTest.keyClick(grid, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
            QCoreApplication.processEvents()
        _wait(lambda: columns() < small_columns, 3)
        keyboard_width, keyboard_columns = grid.thumbnail_width, columns()
        QTest.keyClick(grid, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
        assert grid.thumbnail_width < keyboard_width, 'Ctrl+minus did not shrink overview thumbnails'
        for _ in range(16):
            if columns() > keyboard_columns:
                break
            QTest.keyClick(grid, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
            QCoreApplication.processEvents()
        _wait(lambda: columns() > keyboard_columns, 3)
        assert not clicked and window._current_id == original_id
        assert window._grid_mode and window.content.currentWidget() is grid
        assert not window.viewer._play_requested, 'Overview zoom unexpectedly started video playback'

        grid.verticalScrollBar().setValue(0)
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
        assert not clicked and window._current_id == original_id
        return {'pinch_larger_width': large_width, 'pinch_smaller_width': small_width,
                'columns_before': before_columns, 'columns_larger': large_columns,
                'columns_smaller': small_columns, 'pinch_changes_size_and_columns': True,
                'ctrl_plus_minus_changes_size_and_columns': True,
                'zoom_does_not_open_or_play_photo': True, 'single_finger_scroll_retained': True}
    finally:
        grid.clicked.disconnect(record_click)
        grid.setModel(original_model)
        grid.set_thumbnail_width(original_width)
        scrolling_model.deleteLater()


def _check_filmstrip_touch(window, report_dir: Path):
    from .touch_filmstrip import TouchFilmstrip

    strip = window.strip
    assert isinstance(strip, TouchFilmstrip)
    original_id = window._current_id
    remembered = window._remembered_strip
    window._toggle_strip(True)
    bar = strip.horizontalScrollBar()
    _wait(lambda: strip.isVisible() and bar.maximum() > 0, 3)
    clicked = []
    def record_click(index):
        clicked.append(index.row())
    strip.clicked.connect(record_click)
    try:
        bar.setValue(0)
        QCoreApplication.processEvents()
        device = QTest.createTouchDevice()
        gesture = QTest.touchEvent(strip.viewport(), device, False)
        center = strip.viewport().rect().center()
        distance = min(160, strip.viewport().width() // 4)
        right, left = center + QPoint(distance, 0), center - QPoint(distance, 0)
        gesture.press(0, right, strip.viewport()).commit()
        gesture.move(0, center, strip.viewport()).commit()
        gesture.release(0, left, strip.viewport()).commit()
        QCoreApplication.processEvents()
        after_left = bar.value()
        assert after_left > 0, 'Filmstrip touch swipe did not scroll horizontally'
        gesture.press(0, left, strip.viewport()).commit()
        gesture.move(0, center, strip.viewport()).commit()
        gesture.release(0, right, strip.viewport()).commit()
        QCoreApplication.processEvents()
        assert bar.value() < after_left, 'Reverse filmstrip swipe did not scroll back'
        assert not clicked and window._current_id == original_id, 'Filmstrip swipe changed the current photo'
        assert not window.viewer._play_requested, 'Filmstrip swipe started playback'

        bar.setValue(0)
        QCoreApplication.processEvents()
        row = next(row for row, item in enumerate(window.model.items)
                   if item.id != original_id and strip.viewport().rect().contains(
                       strip.visualRect(window.model.index(row)).center()))
        expected_id = window.model.items[row].id
        position = strip.visualRect(window.model.index(row)).center()
        gesture.press(0, position, strip.viewport()).commit()
        gesture.release(0, position, strip.viewport()).commit()
        QCoreApplication.processEvents()
        assert clicked == [row] and window._current_id == expected_id, 'Filmstrip tap did not select exactly once'
        _wait(lambda: window._displayed_item is not None and window._displayed_item.id == expected_id, 18)
        assert not window.viewer._play_requested
        window.grab().save(str(report_dir / 'filmstrip-touch.png'))
        return {'horizontal_scroll_both_directions': True, 'swipe_keeps_current_photo': True,
                'tap_selects_once': True, 'no_unexpected_playback': True}
    finally:
        strip.clicked.disconnect(record_click)
        original_row = next(row for row, item in enumerate(window.model.items) if item.id == original_id)
        window._select_row(original_row, allow_autoplay=False)
        window._toggle_strip(remembered)


def _check_filmstrip_resize(window, report_dir: Path):
    handle = window.film_resize_handle
    original_id = window._current_id
    original_height = window.preferences.filmstrip_height
    remembered = window._remembered_strip
    portrait_row = next(row for row, item in enumerate(window.model.items)
                        if item.image_path.stem == 'Portrait_HEVC')
    portrait_id = window.model.items[portrait_row].id
    window._select_row(portrait_row, allow_autoplay=False)
    window._toggle_strip(True)
    _wait(lambda: window._displayed_item is not None and window._displayed_item.id == portrait_id
          and not window.viewer._photo.pixmap().isNull() and handle.isVisible(), 18)
    window.viewer.fit_image()
    device = QTest.createTouchDevice()

    def state():
        item = window.strip.visualRect(window.model.index(0))
        fitted = window.viewer.mapFromScene(window.viewer._image_rect).boundingRect()
        return {'film': window.film.height(), 'preview_width': item.width(), 'preview_height': item.height(),
                'viewer': window.viewer.viewport().height(), 'fitted_image': fitted.height()}

    def drag(amount, touch=False):
        start = handle.rect().center()
        end = handle.mapToGlobal(start) + QPoint(0, amount)
        if touch:
            gesture = QTest.touchEvent(handle, device, False)
            gesture.press(0, start, handle).commit()
            gesture.move(0, handle.mapFromGlobal(end), handle).commit()
            QCoreApplication.processEvents()
            gesture.release(0, handle.mapFromGlobal(end), handle).commit()
        else:
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(handle, handle.mapFromGlobal(end))
            QCoreApplication.processEvents()
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(end))
        QCoreApplication.processEvents()

    def assert_larger(before):
        def resized():
            current = state()
            return (current['film'] > before['film']
                    and current['preview_width'] > before['preview_width']
                    and current['preview_height'] > before['preview_height']
                    and current['viewer'] < before['viewer']
                    and current['fitted_image'] < before['fitted_image'])
        _wait(resized, 3)
        after = state()
        assert after['preview_width'] > before['preview_width'] and after['preview_height'] > before['preview_height']
        assert after['viewer'] < before['viewer'] and after['fitted_image'] < before['fitted_image']
        assert abs(window.viewer.zoom_factor() - 1) < .01, 'Resizing lost fit-to-window mode'
        assert window._current_id == portrait_id and not window.viewer._play_requested
        return after

    try:
        QCoreApplication.processEvents()
        before = state()
        window.grab().save(str(report_dir / 'filmstrip-resize-before.png'))
        drag(-70)
        mouse_large = assert_larger(before)
        window.grab().save(str(report_dir / 'filmstrip-resize-larger.png'))
        drag(mouse_large['film'] - before['film'])
        _wait(lambda: abs(window.film.height() - before['film']) <= 2
              and state()['viewer'] >= before['viewer'] - 2
              and state()['fitted_image'] >= before['fitted_image'] - 2, 3)
        assert state()['viewer'] >= before['viewer'] - 2 and state()['fitted_image'] >= before['fitted_image'] - 2

        drag(-70, touch=True)
        touch_large = assert_larger(before)
        window._toggle_strip(False)
        _wait(lambda: not window.film.isVisible() and not handle.isVisible()
              and window.viewer.viewport().height() > touch_large['viewer'], 3)
        assert not window.film.isVisible() and not handle.isVisible()
        assert window.viewer.viewport().height() > touch_large['viewer']
        window._toggle_strip(True)
        QCoreApplication.processEvents()
        assert window.film.isVisible() and handle.isVisible()
        window._toggle_fullscreen()
        QCoreApplication.processEvents()
        assert window._fullscreen_active and window.film.isVisible()
        window._toggle_fullscreen()
        QCoreApplication.processEvents()
        assert not window._fullscreen_active and window.film.isVisible() and handle.isVisible()
        drag(window.film.height() - before['film'], touch=True)
        _wait(lambda: abs(window.film.height() - before['film']) <= 2
              and state()['viewer'] >= before['viewer'] - 2
              and state()['fitted_image'] >= before['fitted_image'] - 2, 3)
        assert state()['preview_height'] <= before['preview_height'] + 2
        assert state()['viewer'] >= before['viewer'] - 2 and state()['fitted_image'] >= before['fitted_image'] - 2
        return {'mouse_and_touch_resize': True, 'preview_tracks_filmstrip_height': True,
                'viewer_space_and_fit_update': True, 'reverse_drag_restores_layout': True,
                'hidden_and_fullscreen_remain_usable': True}
    finally:
        if window._fullscreen_active:
            window._toggle_fullscreen()
        window._set_film_height(original_height)
        row = next(row for row, item in enumerate(window.model.items) if item.id == original_id)
        window._select_row(row, allow_autoplay=False)
        window._toggle_strip(remembered)


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
        assert len(navigation) == 6 and navigation[-1] == 1 and abs(viewer.zoom_factor() - 1) < .005
        viewer.set_options(horizontal_scroll_mode='navigate')
        return {'blank_swipe': [1, -1], 'horizontal_angle_and_pixel': [1, -1, 1, -1],
                'default_navigate_when_zoomed': True, 'blank_hold_starts_video': False,
                'zoomed_pan_and_vertical_zoom': True, 'fit_horizontal_wheel_always_navigates': True}
    finally:
        viewer.navigate_requested.disconnect(navigation.append)


def _check_zoom_navigation_overview(viewer: ViewerCanvas, fixtures: Path, report_dir: Path):
    from unittest.mock import patch

    viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'), fixtures / 'Portrait_HEVC.MOV')
    navigator = viewer.zoom_navigator
    assert not navigator.isVisible()
    viewer._zoom_at(2.0, viewer._viewport_center())
    assert navigator.isVisible()
    image, visible = navigator.image_rect(), navigator.visible_rect()
    scene = viewer._image_rect
    clipped = viewer.mapToScene(viewer.viewport().rect()).boundingRect().intersected(scene)
    expected = (clipped.width() / scene.width(), clipped.height() / scene.height())
    assert abs(visible.width() / image.width() - expected[0]) < 1e-5
    assert abs(visible.height() / image.height() - expected[1]) < 1e-5
    revision = navigator.thumbnail_revision
    viewer._pan_by(QPointF(70, 55))
    assert navigator.visible_rect() != visible and navigator.thumbnail_revision == revision
    with patch.object(viewer._player, 'play', wraps=viewer._player.play) as playback:
        point = QPoint(round(image.center().x()), round(image.top() + image.height() * .85))
        before = viewer._scene_center()
        QTest.mouseClick(navigator, Qt.MouseButton.LeftButton, pos=point)
        assert (viewer._scene_center() - before).manhattanLength() > .5
        target = viewer.window().windowHandle()
        assert target is not None and target.isExposed()
        device = QTest.createTouchDevice()
        touch = QTest.touchEvent(target, device, False)
        point = navigator.visible_rect().center().toPoint()
        start = navigator.mapTo(viewer.window(), point)
        end = navigator.mapTo(viewer.window(), point + QPoint(0, -18))
        before = viewer._scene_center()
        touch.press(0, start, target).commit()
        touch.move(0, end, target).commit()
        touch.release(0, end, target).commit()
        QCoreApplication.processEvents()
        assert (viewer._scene_center() - before).manhattanLength() > .5
        assert playback.call_count == 0 and not viewer._play_requested
    assert navigator.thumbnail_revision == revision
    assert navigator.pos() == QPoint(max(0, viewer.viewport().width() - navigator.width() - 12),
                                     max(0, viewer.viewport().height() - navigator.height() - 12))
    viewer.grab().save(str(report_dir / 'zoom-navigation-overview.png'))
    navigator.grab().save(str(report_dir / 'zoom-navigation-detail.png'))
    viewer.fit_image()
    assert not navigator.isVisible()
    return {'only_visible_when_zoomed': True, 'visible_region_matches_scene': True,
            'rectangle_tracks_pan': True, 'thumbnail_reused_without_redecode': True,
            'mouse_and_touch_can_reposition': True, 'navigation_does_not_play_live': True,
            'fixed_bottom_right': True, 'logical_maximum_size': [160, 160],
            'physical_touch_hardware': False}


def _check_zoomed_double_tap_drag(viewer: ViewerCanvas, fixtures: Path, *, diagnostics=None,
                                align_cursor=True, diagnostics_path: Path | None = None):
    from unittest.mock import patch
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtGui import QCursor, QInputDevice, QMouseEvent, QPointingDevice

    original_hold = viewer._hold_ms
    results = []
    hold = 150
    target = viewer.window().windowHandle()
    assert target is not None and target.isExposed()
    viewer.activateWindow()
    viewer.setFocus()
    QCoreApplication.processEvents()
    trace = diagnostics if diagnostics is not None else {}
    trace.update(align_cursor=align_cursor, qt_platform=QApplication.platformName(), events=[], snapshots=[])
    route, device, position = 'initial', None, viewer.viewport().rect().center()

    def identity(widget):
        return None if widget is None else {'class': type(widget).__name__, 'name': widget.objectName()}

    def device_identity(pointer):
        return None if pointer is None else {'name': pointer.name(), 'id': pointer.systemId(), 'type': str(pointer.type())}

    def state(phase):
        cursor = QCursor.pos()
        local = viewer.viewport().mapFromGlobal(cursor)
        return {'phase': phase, 'route': route, 'time': time.monotonic(),
                'last_time': viewer._last_mouse_click_time, 'last_timestamp': viewer._last_mouse_click_timestamp,
                'last_device': device_identity(viewer._last_mouse_click_device),
                'route_device': device_identity(device),
                'same_device': device is not None and viewer._last_mouse_click_device == device,
                'mouse_down': viewer._mouse_down, 'mouse_moved': viewer._mouse_moved,
                'double_drag': viewer._mouse_double_drag, 'hold_consumed': viewer._hold_consumed,
                'hold_timer': viewer._hold_timer.isActive(), 'play_requested': viewer._play_requested,
                'cursor_global': [cursor.x(), cursor.y()], 'cursor_local': [local.x(), local.y()],
                'cursor_inside': viewer.viewport().rect().contains(local),
                'injected_local': [position.x(), position.y()],
                'focus': identity(QApplication.focusWidget()), 'active_window': identity(QApplication.activeWindow()),
                'mouse_grabber': identity(viewer.mouseGrabber()), 'window_active': target.isActive(),
                'window_exposed': target.isExposed()}

    def snapshot(phase, event=None):
        value = state(phase)
        if event is not None:
            value['input'] = {'type': event.type().name, 'timestamp': event.timestamp(),
                              'source': event.source().name,
                              'device': device_identity(event.pointingDevice()),
                              'local': [event.position().x(), event.position().y()],
                              'global': [event.globalPosition().x(), event.globalPosition().y()]}
        trace['snapshots'].append(value)

    class InputTrace(QObject):
        def eventFilter(self, watched, event):
            if len(trace['events']) < 500 and watched in (viewer, viewer.viewport(), target) and event.type() in (
                    QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                    QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate, QEvent.Type.GrabMouse, QEvent.Type.UngrabMouse,
                    QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick,
                    QEvent.Type.MouseMove):
                value = state(event.type().name)
                value['receiver'] = identity(watched)
                if isinstance(event, QMouseEvent):
                    value['input'] = {'timestamp': event.timestamp(), 'source': event.source().name,
                                      'device': device_identity(event.pointingDevice()),
                                      'local': [event.position().x(), event.position().y()],
                                      'global': [event.globalPosition().x(), event.globalPosition().y()]}
                trace['events'].append(value)
            return False

    observer = InputTrace()
    QApplication.instance().installEventFilter(observer)
    original_stop = viewer.stop_live

    def traced_stop():
        if len(trace['events']) < 500:
            value = state('stop_live')
            value['stack'] = [f'{frame.name}:{frame.lineno}' for frame in traceback.extract_stack(limit=7)]
            trace['events'].append(value)
        return original_stop()

    def mouse(event_type, position, *, timestamp=0, device=None,
              source=Qt.MouseEventSource.MouseEventNotSynthesized):
        # QTest.mouseDClick(QWindow) sends a complete double-click sequence;
        # adding a preceding click would create a third click and reverse zoom.
        # Send the two precise driver variants through the real viewport instead.
        moving = event_type == QEvent.Type.MouseMove
        released = event_type == QEvent.Type.MouseButtonRelease
        event = QMouseEvent(event_type, QPointF(position), QPointF(position),
                            QPointF(viewer.viewport().mapToGlobal(position)),
                            Qt.MouseButton.NoButton if moving else Qt.MouseButton.LeftButton,
                            Qt.MouseButton.NoButton if released else Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier, source,
                            device or QPointingDevice.primaryPointingDevice())
        event.setTimestamp(timestamp)
        snapshot(f'before-send-{event_type.name}-{timestamp}', event)
        QApplication.sendEvent(viewer.viewport(), event)
        snapshot(f'after-send-{event_type.name}-{timestamp}', event)
        QCoreApplication.processEvents()
        snapshot(f'after-process-{event_type.name}-{timestamp}', event)

    try:
        with patch.object(viewer, 'stop_live', side_effect=traced_stop), \
                patch.object(viewer._player, 'play', wraps=viewer._player.play) as playback:
            known_pad = QPointingDevice('self-test touchpad', 71010, QInputDevice.DeviceType.TouchPad,
                                         QPointingDevice.PointerType.Generic,
                                         QInputDevice.Capability.Position, 1, 3)
            routes = ('second-press', 'double-click', 'native-without-history',
                      'system-double-click', 'delayed-input-timestamp', 'known-touchpad-drift')
            for route in routes:
                viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'), fixtures / 'Portrait_HEVC.MOV')
                viewer.set_options(hold_ms=hold)
                center = viewer.viewport().rect().center()
                viewer._zoom_at(2.0, QPointF(center))
                position = center
                device = known_pad if route == 'known-touchpad-drift' else QPointingDevice.primaryPointingDevice()
                if align_cursor:
                    # Explicit QMouseEvent injection does not move the native
                    # cursor. Establish a consistent real-window input target;
                    # this is a test precondition, not a product gesture fix.
                    QTest.mouseMove(viewer.viewport(), center)
                    QCoreApplication.processEvents()
                snapshot('route-ready')
                if align_cursor:
                    ready = trace['snapshots'][-1]
                    assert ready['cursor_inside'] and ready['cursor_local'] == ready['injected_local'], \
                        f'{route}: native cursor does not match the injected starting point'
                    assert ready['window_active'] and ready['window_exposed'], \
                        f'{route}: input window is not active and exposed'
                before_scale, before_center = viewer.transform().m11(), viewer._scene_center()
                hints = QApplication.styleHints()
                mouse_distance = 4 if route == 'known-touchpad-drift' else hints.mouseDoubleClickDistance()
                touch_distance = 40 if route == 'known-touchpad-drift' else hints.touchDoubleTapDistance()
                source = (Qt.MouseEventSource.MouseEventSynthesizedBySystem if route == 'system-double-click'
                          else Qt.MouseEventSource.MouseEventNotSynthesized)
                with patch.object(hints, 'mouseDoubleClickDistance', return_value=mouse_distance), \
                        patch.object(hints, 'touchDoubleTapDistance', return_value=touch_distance):
                    if route != 'native-without-history':
                        mouse(QEvent.Type.MouseButtonPress, position, timestamp=1000, device=device)
                        mouse(QEvent.Type.MouseButtonRelease, position, timestamp=1020, device=device)
                    if route == 'delayed-input-timestamp':
                        snapshot('before-delayed-wait')
                        QTest.qWait(QApplication.doubleClickInterval() + 40)
                        snapshot('after-delayed-wait')
                    if route == 'known-touchpad-drift':
                        position += QPoint(12, 0)
                    if route == 'system-double-click':
                        mouse(QEvent.Type.MouseButtonPress, position, timestamp=1100, device=device)
                    second_kind = (QEvent.Type.MouseButtonDblClick if route in (
                        'double-click', 'native-without-history', 'system-double-click') else QEvent.Type.MouseButtonPress)
                    mouse(second_kind, position, timestamp=1100, device=device, source=source)
                    mouse(QEvent.Type.MouseMove, position + QPoint(1, 0), device=device, source=source)
                    QTest.qWait(hold + 80)
                    snapshot('after-second-hold-pause')
                    assert playback.call_count == 0 and not viewer._play_requested, f'{route} briefly requested Live playback'
                    assert viewer._photo.isVisible() and not viewer._video.isVisible()
                    mouse(QEvent.Type.MouseMove, position + QPoint(80, 35), device=device, source=source)
                    mouse(QEvent.Type.MouseButtonRelease, position + QPoint(80, 35), device=device, source=source)
                QCoreApplication.processEvents()
                assert abs(viewer.transform().m11() - before_scale) < 1e-6, 'Tap-to-drag changed the zoom'
                assert (viewer._scene_center() - before_center).manhattanLength() > .5, 'Tap-to-drag did not pan'
                assert not viewer.is_zoom_animating() and not viewer._click_timer.isActive()
                results.append({'route': route, 'pause_before_drag_ms': hold + 80,
                                'play_calls': playback.call_count, 'zoom_preserved': True, 'panned': True,
                                'native_cursor_aligned': align_cursor,
                                'input_timestamps_ms': [1000, 1020, 1100] if route != 'native-without-history' else [1100],
                                'input_device': 'explicit touchpad fixture' if device is known_pad else 'primary pointer'})
            center = viewer.viewport().rect().center()
            position = center
            mouse(QEvent.Type.MouseButtonPress, position)
            mouse(QEvent.Type.MouseButtonRelease, position)
            mouse(QEvent.Type.MouseButtonDblClick, position)
            mouse(QEvent.Type.MouseButtonRelease, position)
            assert viewer.is_zoom_animating(), 'Deliberate double-click lost its zoom animation'
            _wait(lambda: not viewer.is_zoom_animating(), 3)
            assert abs(viewer.zoom_factor() - 1.0) < .005
            assert playback.call_count == 0
            viewer.set_options(hold_ms=original_hold)
            mouse(QEvent.Type.MouseButtonPress, position)
            _wait(lambda: playback.call_count == 1, 5)
            assert viewer._hold_timer.interval() == original_hold
            mouse(QEvent.Type.MouseButtonRelease, position)
            viewer.set_photo(_qimage(fixtures / 'Portrait_HEVC.jpg'), fixtures / 'Portrait_HEVC.MOV')
            viewer._zoom_at(2.0, QPointF(center))
            before_scale, before_center = viewer.transform().m11(), viewer._scene_center()
            device = QTest.createTouchDevice()
            gesture = QTest.touchEvent(target, device, False)
            first = viewer.viewport().mapTo(viewer.window(), center)
            second = first + QPoint(5, 0)
            gesture.press(0, first, target).commit()
            gesture.release(0, first, target).commit()
            gesture.press(0, second, target).commit()
            QTest.qWait(original_hold + 80)
            assert playback.call_count == 1 and not viewer._play_requested, 'Raw second touch started Live during its pause'
            assert not viewer._hold_timer.isActive(), 'Raw second contact retained a Live hold timer'
            gesture.move(0, second + QPoint(80, 35), target).commit()
            gesture.release(0, second + QPoint(80, 35), target).commit()
            assert abs(viewer.transform().m11() - before_scale) < 1e-6
            assert (viewer._scene_center() - before_center).manhattanLength() > .5
            assert not viewer.is_zoom_animating() and not viewer._click_timer.isActive()
            gesture.press(0, first, target).commit()
            _wait(lambda: playback.call_count == 2, 5)
            gesture.release(0, first, target).commit()
        return {'routes': results, 'no_transient_live_request': True, 'zoom_preserved': True,
                'panned': True, 'ordinary_double_click_zoom_preserved': True,
                'event_injection': 'Explicit QMouseEvent press/release/second-press or DoubleClick',
                'native_history_independent': True, 'same_device_input_timestamps': True,
                'known_touchpad_drift_guard': True, 'raw_second_contact_no_hold': True,
                'raw_touch_qwindow_injection': True, 'raw_pause_before_drag_ms': original_hold + 80,
                'native_cursor_aligned': align_cursor,
                'mouse_guard_hold_delay_ms': hold, 'raw_hold_delay_ms': original_hold,
                'independent_mouse_and_touch_hold_play': True, 'independent_hold_delay_ms': original_hold,
                'hold_setting_unchanged': True, 'physical_touchpad_hardware': False}
    except Exception as error:
        snapshot('failed')
        trace.update(error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
        print('GESTURE_DIAGNOSTICS ' + json.dumps(trace, ensure_ascii=True), flush=True)
        raise
    finally:
        QApplication.instance().removeEventFilter(observer)
        if diagnostics_path is not None:
            diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostics_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding='utf-8')
        viewer.stop_live()
        viewer.set_options(hold_ms=original_hold)
        viewer.fit_image()


def _check_blank_touch_context_menu(viewer: ViewerCanvas, report_dir: Path):
    """Exercise the opening contact through a real popup's nested event loop."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QTouchEvent
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QMenu

    viewer.stop_live()
    viewer.fit_image()
    viewer.show()
    viewer.activateWindow()
    viewer.setFocus()
    QCoreApplication.processEvents()
    window = viewer.window()
    target = window.windowHandle()
    assert target is not None and target.isExposed(), 'Touch menu needs an exposed window'
    left = viewer.mapFromScene(viewer._image_rect.topLeft()).x()
    point = QPoint(max(10, left - 30), viewer.viewport().height() // 2)
    assert not viewer.image_contains_viewport_point(QPointF(point)), 'Touch menu needs a blank letterbox'
    position = viewer.viewport().mapTo(window, point)
    device = QTest.createTouchDevice()
    original_policy = viewer.contextMenuPolicy()
    viewer.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    routes = []
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        for choice in ('touch', 'mouse'):
            sequence = QTest.touchEvent(target, device, False)
            result = {'next_input': choice, 'held_after_menu_ms': 750,
                      'explicit_window_target': True, 'opened': 0}
            routes.append(result)

            def popup(context_position):
                result['opened'] += 1
                viewer.stop_live()  # The main window does this before menu.exec.
                menu = QMenu(viewer)
                action = menu.addAction('选择测试项')
                triggered = QSignalSpy(action.triggered)
                release_timer = QTimer(menu)
                release_timer.setSingleShot(True)
                deadline = QTimer(menu)
                deadline.setSingleShot(True)

                def release_contact():
                    try:
                        for offset in (4, 8, 12, 16):
                            sequence.move(0, position + QPoint(offset, 0), target).commit()
                        result['visible_while_held'] = menu.isVisible()
                        sequence.release(0, position + QPoint(16, 0), target).commit()
                        QCoreApplication.processEvents()
                        result['visible_after_release'] = menu.isVisible()
                        result['accidental_choices'] = triggered.count()
                        assert menu.isVisible(), f'Opening touch dismissed its menu: {result}'
                        assert triggered.count() == 0, f'Opening touch selected an item: {result}'
                        screenshot = f'blank-touch-menu-{choice}.png'
                        assert menu.grab().save(str(report_dir / screenshot))
                        result['screenshot'] = screenshot
                        item_point = menu.actionGeometry(action).center()
                        if choice == 'touch':
                            menu_target = menu.windowHandle()
                            assert menu_target is not None
                            tap = QTest.touchEvent(menu_target, device, False)
                            tap.press(0, item_point, menu_target).commit()
                            tap.release(0, item_point, menu_target).commit()
                        else:
                            # QMenu ignores a click exactly at its saved cursor
                            # origin. The previous touch route can leave that
                            # origin at the same row's center, so choose another
                            # real point inside the row on this second route.
                            item_point.setX(menu.actionGeometry(action).right() - 8)
                            QTest.mouseMove(menu, item_point)
                            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=item_point)
                        QCoreApplication.processEvents()
                        result['deliberate_choices'] = triggered.count()
                    except Exception:
                        result['callback_error'] = traceback.format_exc()
                        menu.close()

                def timed_out():
                    result['timed_out'] = True
                    menu.close()

                release_timer.timeout.connect(release_contact)
                deadline.timeout.connect(timed_out)
                release_timer.start(750)
                deadline.start(3000)
                try:
                    menu.exec(viewer.viewport().mapToGlobal(context_position))
                    result['menu_returned'] = True
                finally:
                    release_timer.stop()
                    deadline.stop()
                    menu.close()
                    menu.deleteLater()

            viewer.customContextMenuRequested.connect(popup)
            try:
                QCoreApplication.processEvents()
                # Keep QMenu's saved cursor origin away from the later choice,
                # including when the preceding route ended with a touch tap.
                QTest.mouseMove(target, viewer.viewport().mapTo(
                    window, QPoint(viewer.viewport().width() - 15, 15)))
                sequence.press(0, position, target).commit()
                assert viewer._blank_hold_timer.isActive(), 'Blank long press did not start its timer'
                _wait(lambda: result.get('menu_returned'), 6)
                assert 'callback_error' not in result, result.get('callback_error')
                assert not result.get('timed_out'), f'Menu did not accept deliberate {choice}: {result}'
                assert result['opened'] == 1 and result.get('visible_while_held'), result
                assert result.get('visible_after_release') and result.get('accidental_choices') == 0, result
                assert result.get('deliberate_choices') == 1, result
                assert not viewer._play_requested and not viewer._click_timer.isActive()
            finally:
                viewer.customContextMenuRequested.disconnect(popup)
                QApplication.sendEvent(target, QTouchEvent(QEvent.Type.TouchCancel, device))
                viewer.stop_live()
                QCoreApplication.processEvents()
        return {'routes': routes, 'physical_touch_hardware': False,
                'original_release_keeps_menu': True, 'no_accidental_choices': True,
                'next_touch_and_mouse_choose_once': True}
    finally:
        viewer.setContextMenuPolicy(original_policy)
        (report_dir / 'blank-touch-menu-diagnostics.json').write_text(
            json.dumps(routes, ensure_ascii=False, indent=2), encoding='utf-8')


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
    options = (viewer._loop, viewer._audio.isMuted(), viewer._preserve_zoom,
               viewer._hold_ms, viewer._horizontal_scroll_mode)
    viewer.navigate_requested.connect(navigation.append)
    try:
        viewer.set_options(*options[:4], horizontal_scroll_mode='pan')
        viewer.fit_image()
        QTest.qWait(220)
        user32.SendMessageW(hwnd, 0x020E, 120 << 16, packed)  # WM_MOUSEHWHEEL: right
        _wait(lambda: bool(navigation), 3)
        assert navigation == [1], f'Windows right wheel did not advance once: {navigation}'
    finally:
        viewer.navigate_requested.disconnect(navigation.append)
        viewer.set_options(*options)


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


def _check_exact_frame_reader(fixtures: Path, report_dir: Path):
    results = []
    for filename in ('Live_H264.MOV', 'Live_HEVC.MOV', 'Portrait_HEVC.MOV', 'VFR_PAIR.MOV'):
        with FrameReader(fixtures / filename) as reader:
            metadata = reader.metadata
            expected_count = 7 if filename == 'VFR_PAIR.MOV' else 21
            assert reader.frame_count == expected_count, f'Incorrect frame count for {filename}'
            assert not metadata.timestamps_estimated
            preview = reader.preview(0, edge=256)
            assert max(preview.size) <= 256
            first = reader.export(0)
            assert first.size == (metadata.width, metadata.height)
            if filename == 'Portrait_HEVC.MOV':
                assert first.size == (420, 640)
                rgba = first.convert('RGBA')
                image = QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format.Format_RGBA8888).copy()
                _assert_portrait_corners(image)
            elif filename == 'VFR_PAIR.MOV':
                from PIL import ImageColor
                expected_times = (0, 40, 160, 200, 440, 480, 760)
                assert [round(frame.pts_seconds * 1000) for frame in metadata.frames] == list(expected_times)
                colors = ('#d43d35', '#42bd51', '#3568d4', '#e2d63c', '#cf42c2', '#3fc2d1', '#df8d32')
                for index, color in enumerate(colors):
                    selected = reader.export(index)
                    actual, expected = selected.getpixel((160, 120)), ImageColor.getrgb(color)
                    assert max(abs(a - b) for a, b in zip(actual, expected)) < 8, f'VFR frame {index} is wrong'
                assert reader.nearest_index(140) == 2
            else:
                for index, x in ((0, 140), (9, 230), (20, 340)):
                    selected = reader.export(index)
                    r, g, b = selected.getpixel((x, 180))
                    assert r > 180 and g > 120 and b < 170, f'{filename} frame {index} is wrong'
                assert first.getpixel((340, 180))[0] < 150
            first.save(report_dir / f'exact-frame-{Path(filename).stem}.png')
            results.append({'file': filename, 'frame_count': reader.frame_count,
                            'display_size': [metadata.width, metadata.height],
                            'pts_milliseconds': [round(frame.pts_seconds * 1000) for frame in metadata.frames],
                            'status': 'passed'})
    return results


def _check_windows_explorer_selection(report_dir: Path):
    """Request one real selection, then observe Explorer independently.

    Hosted Windows desktops can lack an interactive Explorer session. A native
    request must still succeed; the report distinguishes a verified selection
    from a desktop that could not expose its state to Shell automation.
    """
    import threading
    from PIL import Image
    from .reveal_file import reveal_in_folder

    script = r'''
$ErrorActionPreference = 'Stop'
$target = $env:LIVEVIEWER_REVEAL_TARGET
$deadline = [DateTime]::UtcNow.AddSeconds(10)
$shell = $null
$verified = $false
$reason = 'Explorer did not expose the selected item within the time limit.'
try {
    $shell = New-Object -ComObject Shell.Application
    do {
        $windows = $shell.Windows()
        for ($i = 0; $i -lt $windows.Count; $i++) {
            try {
                $window = $windows.Item($i)
                $items = $window.Document.SelectedItems()
                for ($j = 0; $j -lt $items.Count; $j++) {
                    $selectedPath = [String]($items.Item($j).Path)
                    if ([String]::Equals($selectedPath, $target,
                                        [StringComparison]::OrdinalIgnoreCase)) {
                        $verified = $true
                        $window.Quit() # Close only this owned temporary test window.
                        break
                    }
                }
                if ($verified) { break }
            } catch { }
        }
        if (-not $verified) { Start-Sleep -Milliseconds 150 }
    } until ($verified -or [DateTime]::UtcNow -ge $deadline)
} catch {
    $reason = 'Shell automation was unavailable on this desktop.'
} finally {
    if ($null -ne $shell) {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
}
@{selection_verified=$verified; reason=$(if ($verified) {''} else {$reason})} | ConvertTo-Json -Compress
'''
    with tempfile.TemporaryDirectory(prefix='explorer-select-', dir=report_dir) as temporary:
        folder = Path(temporary)
        target = folder / '旅行,图片 01.png'
        Image.new('RGB', (80, 60), '#486a87').save(target)
        errors = []
        def reveal():
            try:
                assert reveal_in_folder(target)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=reveal, daemon=True)
        worker.start()
        _wait(lambda: not worker.is_alive(), 15)
        if errors:
            raise errors[0]
        script_path = folder / 'observe-selection.ps1'
        script_path.write_text(script, encoding='utf-8-sig')
        environment = os.environ.copy()
        environment['LIVEVIEWER_REVEAL_TARGET'] = str(target.resolve())
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        try:
            process = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Sta',
                                      '-ExecutionPolicy', 'Bypass', '-File', str(script_path)],
                                     env=environment, capture_output=True, timeout=18,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            content = process.stdout.decode('utf-8-sig', 'replace').strip()
            observation = json.loads(content[content.find('{'):]) if process.returncode == 0 else {
                'selection_verified': False, 'reason': 'Shell observation process was unavailable.'}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            observation = {'selection_verified': False, 'reason': 'Shell observation process was unavailable.'}
        return {'native_request_accepted': True, 'unicode_and_comma_filename': True,
                **observation}


def _file_digest(path: Path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _cover_movie_media_signature(path: Path):
    """Use bundled FFprobe, independent of our metadata parser, to check A/V."""
    from .frame_tools import resolve_frame_tools
    entries = ('stream=index,codec_type,codec_name,codec_tag_string,width,height,pix_fmt,time_base,'
               'start_pts,duration_ts,sample_rate,channels,channel_layout:stream_tags:stream_side_data:'
               'packet=stream_index,pts,dts,duration,size,data_hash')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    process = subprocess.run([str(resolve_frame_tools().ffprobe), '-v', 'error', '-show_streams',
                              '-show_packets', '-show_data_hash', 'sha256', '-show_entries', entries,
                              '-of', 'json', str(path)], capture_output=True, timeout=30, check=True, **options)
    data = json.loads(process.stdout)
    result = []
    for stream in data['streams']:
        if stream.get('codec_type') not in ('video', 'audio'):
            continue
        index = stream.pop('index')
        packets = [{key: value for key, value in packet.items() if key != 'stream_index'}
                   for packet in data.get('packets', ()) if packet['stream_index'] == index]
        assert packets and all('data_hash' in packet for packet in packets)
        result.append((stream, packets))
    assert result, 'Live Photo fixture has no audio/video packets'
    return result


def _check_heic_cover_roundtrip(fixtures: Path, report_dir: Path):
    from .cover_save import CoverSource, save_cover
    from .livephoto_metadata import photo_identifier, validate_pair
    with tempfile.TemporaryDirectory(prefix='heic-cover-', dir=report_dir) as temporary:
        folder = Path(temporary)
        still, video = folder / 'Real_HEIC.heic', folder / 'Real_HEIC.MOV'
        shutil.copy2(fixtures / 'Live_H264.heic', still)
        shutil.copy2(fixtures / 'Live_H264.MOV', video)
        item = MediaItem('heic-roundtrip', still, video)
        before_image, before_video = _file_digest(still), _file_digest(video)
        before_media = _cover_movie_media_signature(video)
        with FrameReader(video) as reader:
            selected = reader.export(0)
            frame = reader.metadata.frames[0]
            seconds = frame.pts_seconds + reader.metadata.start_time_seconds
        timing = dict(frame_time_seconds=seconds, frame_duration_seconds=frame.duration_seconds)
        copied = save_cover(CoverSource.capture(item), selected, 'copy', **timing)
        assert copied.image_path.suffix == '.heic' and copied.image_path.stem == copied.video_path.stem
        assert _file_digest(still) == before_image and _file_digest(video) == before_video
        assert _cover_movie_media_signature(copied.video_path) == before_media
        copied_id = photo_identifier(copied.image_path)
        validate_pair(copied.image_path, copied.video_path, copied_id, seconds)
        replaced = save_cover(CoverSource.capture(item), selected, 'overwrite', **timing)
        assert replaced.backup_path is None
        assert _cover_movie_media_signature(video) == before_media
        replaced_id = photo_identifier(still)
        validate_pair(still, video, replaced_id, seconds)
        assert replaced_id != copied_id
        assert _file_digest(still) != before_image
        for path in (copied.image_path, replaced.image_path):
            header = path.read_bytes()[:128]
            assert header[4:8] == b'ftyp' and any(brand in header[8:] for brand in (b'heic', b'heix', b'hevc'))
            decoded = load_image(path, None)
            assert decoded.size == selected.size, 'HEIC roundtrip changed full-resolution dimensions'
            decoded.load()
        return {'format': 'HEVC HEIC', 'size': list(selected.size),
                'copy_and_overwrite': True, 'audio_video_packets_and_tracks_unchanged': True,
                'live_photo_pair_metadata_validated': True, 'selected_frame_time_seconds': seconds}


@contextmanager
def _separate_export_test_directory(folder):
    directory = folder / '另一个导出目录 with spaces'
    directory.mkdir()
    attributes = None
    kernel = None
    if sys.platform == 'win32':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel.GetFileAttributesW.restype = ctypes.c_uint32
        kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel.SetFileAttributesW.restype = ctypes.c_int
        attributes = kernel.GetFileAttributesW(str(directory))
        assert attributes != 0xffffffff
        assert kernel.SetFileAttributesW(str(directory), attributes | 0x1)
    try:
        if kernel is not None:
            proof = directory / 'write-proof.tmp'
            proof.write_bytes(b'Actual write permission despite Windows folder READONLY')
            proof.unlink()
        yield directory
        if kernel is not None:
            assert kernel.GetFileAttributesW(str(directory)) & 0x1, 'Export changed the folder attribute'
    finally:
        if kernel is not None:
            kernel.SetFileAttributesW(str(directory), attributes)


def _check_editor_preview_export(dialog, folder: Path, still: Path, video: Path, report_dir: Path):
    import threading
    from unittest.mock import patch
    from PySide6.QtWidgets import QFileDialog, QMenu, QDialogButtonBox
    from . import cover_editor

    preview = dialog.preview
    assert isinstance(preview, ViewerCanvas)
    before_image, before_video = _file_digest(still), _file_digest(video)
    movies_before = {path.name for path in folder.iterdir() if path.suffix.casefold() == '.mov'}
    selected = dialog.selected_frame_index
    assert selected == 4 and dialog.displayed_frame_index == 4
    locator = dialog.open_export_folder_button
    assert locator.text() == '打开导出文件夹' and not locator.isVisible()
    assert dialog.latest_successful_export_path is None
    viewport = preview.viewport()
    center = viewport.rect().center()
    before_zoom = preview.zoom_factor()
    wheel = QWheelEvent(QPointF(center), QPointF(viewport.mapToGlobal(center)), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QCoreApplication.sendEvent(viewport, wheel)
    QCoreApplication.processEvents()
    assert preview.zoom_factor() > before_zoom, 'Editor preview wheel did not zoom'
    before_pinch = preview.zoom_factor()
    dialog.activateWindow()
    QCoreApplication.processEvents()
    target_window = dialog.windowHandle()
    assert target_window is not None and target_window.isExposed(), 'Editor window must be exposed for touch injection'
    def window_position(position):
        return viewport.mapTo(dialog, position)
    device = QTest.createTouchDevice()
    gesture = QTest.touchEvent(target_window, device, False)
    gesture.press(0, window_position(center - QPoint(40, 0)), target_window).press(
        1, window_position(center + QPoint(40, 0)), target_window).commit()
    gesture.move(0, window_position(center - QPoint(85, 0)), target_window).move(
        1, window_position(center + QPoint(85, 0)), target_window).commit()
    gesture.release(0, window_position(center - QPoint(85, 0)), target_window).release(
        1, window_position(center + QPoint(85, 0)), target_window).commit()
    QCoreApplication.processEvents()
    assert preview.zoom_factor() > before_pinch, 'Editor preview pinch did not zoom'
    before_pan = preview._scene_center()
    gesture.press(0, window_position(center), target_window).commit()
    gesture.move(0, window_position(center + QPoint(55, 25)), target_window).commit()
    gesture.release(0, window_position(center + QPoint(80, 35)), target_window).commit()
    QCoreApplication.processEvents()
    assert (preview._scene_center() - before_pan).manhattanLength() > .5, 'Editor preview did not pan after zoom'
    assert dialog.selected_frame_index == selected and not preview._play_requested
    dialog.grab().save(str(report_dir / 'editor-preview-zoom.png'))
    preview.fit_image()

    reveal_requests = []
    actual_reveal = cover_editor.reveal_in_folder
    def observe_reveal(path):
        reveal_requests.append((Path(path), threading.get_ident()))
        return actual_reveal(path) if sys.platform == 'win32' else True
    with _separate_export_test_directory(folder) as export_directory, \
            patch('livegallery.cover_editor.reveal_in_folder', side_effect=observe_reveal):
        target = export_directory / '导出静帧, chosen image.png'
        errors, choices = [], []
        attempts = [0]
        def choose_file():
            prompt = QApplication.activeModalWidget()
            if not isinstance(prompt, QFileDialog):
                attempts[0] += 1
                if attempts[0] < 200:
                    QTimer.singleShot(25, choose_file)
                    return
                errors.append(AssertionError('导出静帧画面 did not open a SaveAs dialog'))
                if prompt is not None:
                    prompt.reject()
                return
            try:
                assert prompt.acceptMode() == QFileDialog.AcceptMode.AcceptSave
                prompt.setDirectory(str(export_directory))
                png_filter = next(value for value in prompt.nameFilters() if '*.png' in value.lower())
                prompt.selectNameFilter(png_filter)
                prompt.selectFile(str(target))
                choices.append(str(target))
                buttons = prompt.findChild(QDialogButtonBox)
                save = buttons.button(QDialogButtonBox.StandardButton.Save)
                assert save is not None and save.isEnabled(), 'Real SaveAs Save button is disabled'
                QTest.mouseClick(save, Qt.MouseButton.LeftButton)
            except Exception as error:
                errors.append(error)
                prompt.reject()

        def choose_export():
            menu = QApplication.activePopupWidget()
            try:
                assert isinstance(menu, QMenu), 'Editor preview context menu did not open'
                action = next(action for action in menu.actions() if action.text().startswith('导出静帧画面'))
                assert action.isEnabled()
                menu.grab().save(str(report_dir / 'editor-frame-export-menu.png'))
                QTimer.singleShot(0, choose_file)
                QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
            except Exception as error:
                errors.append(error)
                if menu is not None:
                    menu.hide()

        previous_native = QApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
        try:
            QTimer.singleShot(0, choose_export)
            dialog._preview_menu(center)
            _wait(lambda: bool(errors or choices), 6)
        finally:
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, previous_native)
        if errors:
            raise errors[0]
        assert choices == [str(target)]
        _wait(lambda: dialog.last_export_path == target and not dialog._export_busy, 25)
        _wait(lambda: dialog._export_thread is None or not dialog._export_thread.is_alive(), 5)
        assert target.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        image = load_image(target, None)
        assert image.size == (320, 240), 'Frame export used the scaled or zoomed preview resolution'
        expected = (207, 66, 194)
        assert max(abs(a - b) for a, b in zip(image.getpixel((160, 120)), expected)) < 10
        assert _file_digest(still) == before_image and _file_digest(video) == before_video
        assert {path.name for path in folder.iterdir() if path.suffix.casefold() == '.mov'} == movies_before
        assert dialog.saved_result is None and dialog.selected_frame_index == selected
        assert dialog.save_button.isEnabled() and dialog.timeline.isEnabled()
        assert locator.isVisible() and locator.isEnabled()
        assert locator.geometry().bottom() < dialog.save_button.mapTo(dialog, QPoint()).y()
        assert dialog.latest_successful_export_path == target and not reveal_requests
        dialog.timeline.setValue(2)
        _wait(lambda: dialog.displayed_frame_index == 2 and dialog.save_button.isEnabled(), 12)
        QTest.mouseClick(locator, Qt.MouseButton.LeftButton)
        _wait(lambda: not dialog._reveal_busy, 20)
        _wait(lambda: dialog._reveal_thread is None or not dialog._reveal_thread.is_alive(), 5)
        assert dialog.last_reveal_accepted and dialog.last_reveal_path == target
        assert len(reveal_requests) == 1 and reveal_requests[0][0] == target
        assert reveal_requests[0][1] != threading.get_ident()
        assert target.is_file() and _file_digest(target)
        dialog.activateWindow()
        dialog.timeline.setValue(selected)
        _wait(lambda: dialog.displayed_frame_index == selected and dialog.save_button.isEnabled(), 12)
        dialog.grab().save(str(report_dir / 'editor-export-folder-button.png'))
        from .settings import Preferences
        assert Preferences.load().last_export_directory == str(export_directory)
        assert dialog._export_directory == export_directory
        return {'wheel_pinch_and_pan': True, 'context_menu_label': '导出静帧画面',
                'save_as_dialog_exercised': True, 'exported_frame_index': 4, 'full_size': [320, 240],
                'original_media_unchanged': True, 'no_video_copy': True, 'cover_save_not_triggered': True,
                'file_dialog_automation': 'Qt SaveAs actual Save button',
                'real_save_button_clicked': True, 'other_directory_export': True,
                'windows_readonly_directory_accepted': sys.platform == 'win32',
                'remembered_directory_persisted': True,
                'export_folder_button_label': locator.text(), 'export_folder_button_initially_hidden': True,
                'export_folder_button_above_save': True, 'export_folder_actual_button_clicked': True,
                'export_folder_no_automatic_launch': True, 'export_folder_reveals_successful_file_not_timeline': True,
                'export_folder_request_off_gui_thread': True,
                'export_folder_native_request_accepted': sys.platform == 'win32' and dialog.last_reveal_accepted,
                'export_folder_request_fixture_on_nonwindows': sys.platform != 'win32',
                'export_folder_selected_target': str(dialog.last_reveal_path),
                'export_folder_target_existed': target.is_file(), 'export_folder_selection_observed': False}


def _check_real_cover_editor(fixtures: Path, report_dir: Path, parent):
    from .cover_editor import EditorDialog, SaveModeDialog
    from .livephoto_metadata import photo_identifier, validate_pair
    with tempfile.TemporaryDirectory(prefix='editor-vfr-', dir=report_dir) as temporary:
        folder = Path(temporary)
        still, video = folder / 'Editor_VFR.jpg', folder / 'Editor_VFR.MOV'
        shutil.copy2(fixtures / 'VFR_PAIR.jpg', still)
        shutil.copy2(fixtures / 'VFR_PAIR.MOV', video)
        item = MediaItem('vfr-editor', still, video)
        before_image, before_video = _file_digest(still), _file_digest(video)
        before_media = _cover_movie_media_signature(video)
        dialog = EditorDialog(item, parent, initial_export_directory=parent.preferences.last_export_directory)
        dialog.frame_exported.connect(parent._remember_frame_export)
        dialog.setModal(True)
        dialog.show()
        try:
            _wait(lambda: dialog.metadata is not None and dialog.save_button.isEnabled(), 20)
            assert dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
            assert not hasattr(dialog, 'copy_choice'), 'Save options still occupy the frame editor'
            _wait(lambda: dialog.original_position is not None, 20)
            assert dialog.original_position.index == 3, 'Original VFR cover marker did not match the known yellow frame'
            assert dialog.timeline.original_index == 3
            slider = dialog.timeline
            _, groove, handle = slider._geometry()
            def slider_position(index):
                span = max(1, groove.width() - handle.width())
                x = groove.x() + handle.width() / 2 + span * index / slider.maximum()
                return QPoint(round(x), handle.center().y())
            dialog.activateWindow()
            QCoreApplication.processEvents()
            target_window = dialog.windowHandle()
            assert target_window is not None and target_window.isExposed(), 'Editor window must be exposed for touch injection'
            device = QTest.createTouchDevice()
            touch = QTest.touchEvent(target_window, device, False)
            last = handle.center()
            touch.press(0, slider.mapTo(dialog, last), target_window).commit()
            QCoreApplication.processEvents()
            shown_while_pressed = []
            try:
                assert slider.isSliderDown(), 'Touch begin did not capture the cover timeline'
                for index in (1, 2, 4):
                    last = slider_position(index)
                    touch.move(0, slider.mapTo(dialog, last), target_window).commit()
                    _wait(lambda index=index: dialog.selected_frame_index == index
                          and dialog.displayed_frame_index == index and dialog.save_button.isEnabled(), 8)
                    assert slider.isSliderDown(), 'Finger was released before preview changed'
                    shown_while_pressed.append(dialog.displayed_frame_index)
            finally:
                touch.release(0, slider.mapTo(dialog, last), target_window).commit()
                QCoreApplication.processEvents()
            assert shown_while_pressed == [1, 2, 4] and not slider.isSliderDown()
            _wait(lambda: dialog.selected_frame_index == 4 and dialog.save_button.isEnabled(), 20)
            assert abs(dialog.metadata.frames[4].pts_seconds - .44) < .000001
            preview_export = _check_editor_preview_export(dialog, folder, still, video, report_dir)
            dialog.grab().save(str(report_dir / 'cover-editor.png'))
            choices, errors = [], []
            def choose_copy():
                prompt = QApplication.activeModalWidget()
                try:
                    assert isinstance(prompt, SaveModeDialog), 'Save mode prompt did not open'
                    assert prompt.copy_choice.isChecked() and prompt.mode == 'copy'
                    prompt.grab().save(str(report_dir / 'cover-save-options.png'))
                    choices.append(prompt.mode)
                    prompt.accept()
                except Exception as error:
                    errors.append(error)
                    if prompt is not None:
                        prompt.reject()
            QTimer.singleShot(0, choose_copy)
            dialog._save()
            if errors:
                raise errors[0]
            assert choices == ['copy'], 'Saving bypassed the copy/overwrite choice'
            _wait(lambda: dialog.saved_result is not None, 25)
            saved = dialog.saved_result
            assert saved.created_copy and saved.image_path.stem == saved.video_path.stem
            assert _file_digest(still) == before_image and _file_digest(video) == before_video
            assert _cover_movie_media_signature(saved.video_path) == before_media
            validate_pair(saved.image_path, saved.video_path, photo_identifier(saved.image_path), .44)
            decoded = load_image(saved.image_path, None)
            assert decoded.size == (320, 240)
            expected = (207, 66, 194)  # Exact VFR frame 4, not a millisecond/fps approximation.
            assert max(abs(a - b) for a, b in zip(decoded.getpixel((160, 120)), expected)) < 10
            _wait(lambda: not dialog._worker.thread.is_alive(), 5)
        finally:
            dialog.close()
        before_cancel = {path.name: _file_digest(path) for path in folder.iterdir() if path.is_file()}
        cancelled = EditorDialog(item, parent)
        cancelled.setModal(True)
        cancelled.show()
        _wait(lambda: cancelled.metadata is not None, 20)
        cancelled.reject()
        _wait(lambda: not cancelled._worker.thread.is_alive(), 5)
        after_cancel = {path.name: _file_digest(path) for path in folder.iterdir() if path.is_file()}
        assert before_cancel == after_cancel and cancelled.saved_result is None
        return {'vfr_index': 4, 'pts_milliseconds': 440, 'full_size': [320, 240],
                'copy_preserves_originals_and_audio_video_packets': True,
                'live_photo_pair_metadata_validated': True, 'cancel_no_changes': True,
                'save_mode_prompt_after_save': True, 'maximize_button_available': True,
                'original_cover_marker_frame': 3,
                'touch_scrub_updates_before_release': shown_while_pressed,
                'preview_export': preview_export}


def _check_filter_blank_click(window):
    from PySide6.QtWidgets import QStyle, QStyleOptionButton

    control = window.kind_filter.live
    menu = window.kind_filter.menu
    assert menu.isVisible(), 'Multi-select filter menu must be open'
    option = QStyleOptionButton()
    option.initFrom(control)
    indicator = control.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, control)
    spacing = control.style().pixelMetric(QStyle.PixelMetric.PM_CheckBoxLabelSpacing, option, control)
    label_right = indicator.right() + spacing + control.fontMetrics().horizontalAdvance(control.text())
    blank = QPoint(control.width() - 3, control.height() // 2)
    assert blank.x() > max(indicator.right(), label_right) + 4, 'Fixture needs blank space after the filter label'
    original = control.isChecked()
    for expected in (not original, original):
        QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=blank)
        QCoreApplication.processEvents()
        assert control.isChecked() == expected, 'Clicking blank space did not toggle the filter row'
        assert menu.isVisible(), 'Clicking blank space closed the multi-select menu'
    return {'right_blank_click_keeps_menu_open': True}


def _check_filmstrip_layouts(window, report_dir: Path):
    """Exercise dock buttons, real input and persisted geometry in the application."""
    from PySide6.QtCore import QRect
    from PySide6.QtTest import QSignalSpy
    from .app import MainWindow
    from .settings import Preferences

    panel, strip, viewer, model = window.film, window.strip, window.viewer, window.model
    fields = ('filmstrip_orientation', 'filmstrip_side', 'filmstrip_height', 'filmstrip_width')
    original = {name: getattr(window.preferences, name) for name in fields}
    remembered, geometry = window._remembered_strip, window.saveGeometry()
    current_id, generation = window._current_id, window._scan_generation
    original_transform, original_center = viewer.transform(), viewer._scene_center()
    original_fit = viewer._fit_mode
    device = QTest.createTouchDevice()
    restarted = None
    trace_reset = None
    reset_traces = []
    report_dir.mkdir(parents=True, exist_ok=True)

    def settle():
        for _ in range(5):
            QCoreApplication.processEvents()
            QTest.qWait(1)

    def click(button):
        assert button.isVisible() and button.isEnabled()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
        settle()

    def state():
        tile = strip.visualRect(model.index(0))
        return {'film_width': panel.width(), 'film_height': panel.height(),
                'preview_width': tile.width(), 'preview_height': tile.height(),
                'viewer_width': viewer.viewport().width(), 'viewer_height': viewer.viewport().height()}

    def drag(amount, touch=False):
        handle = panel.resize_handle
        horizontal = panel.orientation == 'horizontal'
        direction = 1 if panel.side == 'left' else -1
        delta = QPoint(0, -amount) if horizontal else QPoint(direction * amount, 0)
        start = handle.rect().center()
        end_global = handle.mapToGlobal(start) + delta
        if touch:
            target = window.windowHandle()
            assert target is not None and target.isExposed()
            point = handle.mapTo(window, start)
            sequence = QTest.touchEvent(target, device, False)
            sequence.press(0, point, target).commit()
            sequence.move(0, window.mapFromGlobal(end_global), target).commit()
            settle()
            sequence.release(0, window.mapFromGlobal(end_global), target).commit()
        else:
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(handle, handle.mapFromGlobal(end_global))
            settle()
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(end_global))
        settle()

    try:
        window._toggle_grid(False)
        window._toggle_strip(True)
        window._set_film_layout('horizontal', 'right')
        window._set_film_height(220)
        window._set_film_width(240)
        window.resize(1280, 850)
        window.activateWindow()
        settle()
        # Candidate analysis legitimately replaces catalog rows. Finish the
        # fixture's real pair jobs before measuring layout-only model reuse.
        pending_pairs = [item for item in window._primary_items if item.pending]
        for item in pending_pairs:
            window._request_pair(item, 3)
        _wait(lambda: not any(item.pending for item in window._primary_items)
              and not any(context[0] == 'pair' for context in window._request_context.values()), 25)
        _wait(lambda: bool(model.thumbnails) and not model.pending, 25)
        cached = {path: value.cacheKey() for path, value in model.thumbnails.items()}
        assert cached, 'Filmstrip must have real decoded cached thumbnails'
        resets, selected = QSignalSpy(model.modelReset), QSignalSpy(strip.clicked)
        def trace_reset():
            reset_traces.append({'stack': ''.join(traceback.format_stack(limit=14)),
                                 'pending_pairs': [item.id for item in window._primary_items if item.pending],
                                 'background_pair': window._background_pair_id})
        model.modelReset.connect(trace_reset)
        playback = QSignalSpy(viewer._player.playbackStateChanged)
        viewer.fit_image()
        viewer._zoom_at(2.5, viewer._viewport_center())
        transform, center = viewer.transform(), viewer._scene_center()
        strip.current_scrollbar().setValue(strip.current_scrollbar().maximum() // 2)
        settle()
        anchor = panel.capture_anchor()
        assert anchor is not None

        def preserved(check_anchor=False):
            assert window.model is model and strip.model() is model
            assert window._current_id == current_id and window._scan_generation == generation
            assert resets.count() == 0 and selected.count() == 0, {
                'model_reset_count': resets.count(), 'strip_clicked_count': selected.count(),
                'reset_traces': reset_traces}
            assert all(path in model.thumbnails and model.thumbnails[path].cacheKey() == key
                       for path, key in cached.items()), 'Dock change cleared or decoded cached thumbnails again'
            assert abs(viewer.transform().m11() - transform.m11()) < .00001
            assert (viewer._scene_center() - center).manhattanLength() < 5
            assert not viewer._play_requested
            assert all(str(playback.at(i)[0]).split('.')[-1] != 'PlayingState' for i in range(playback.count()))
            navigator = viewer.zoom_navigator
            assert navigator.isVisible() and viewer.viewport().rect().contains(navigator.geometry())
            if check_anchor:
                rect = strip.visualRect(model.index(anchor['index'].row()))
                assert rect.intersects(strip.viewport().rect()), 'Dock change lost the filmstrip browsing anchor'

        preserved(True)
        assert [button.text() for button in (panel.side_button, panel.hide_button, panel.orientation_button)] == ['左侧', 'X', '右侧']
        assert panel.side_button.x() < panel.hide_button.x() < panel.orientation_button.x()
        window.grab().save(str(report_dir / 'filmstrip-bottom.png'))
        click(panel.orientation_button)
        assert (panel.orientation, panel.side) == ('vertical', 'right')
        assert [button.text() for button in (panel.side_button, panel.orientation_button, panel.hide_button)] == ['左侧', '底部', 'X']
        assert panel.side_button.x() < panel.orientation_button.x() < panel.hide_button.x()
        assert strip.scroll_axis == 'vertical' and strip.horizontalScrollBar().maximum() == 0
        assert strip.verticalScrollBar().mapTo(strip, QPoint()).x() > strip.viewport().geometry().right()
        preserved(True)
        right_before = state()
        drag(42)
        right_mouse = state()
        assert right_mouse['film_width'] > right_before['film_width']
        assert right_mouse['preview_width'] > right_before['preview_width']
        assert right_mouse['preview_height'] > right_before['preview_height']
        assert right_mouse['viewer_width'] < right_before['viewer_width']
        preserved()
        drag(-42)
        _wait(lambda: abs(panel.width() - right_before['film_width']) <= 2, 3)
        drag(38, touch=True)
        right_touch = state()
        assert right_touch['film_width'] > right_before['film_width']
        assert right_touch['preview_height'] > right_before['preview_height']
        preserved()
        window.grab().save(str(report_dir / 'filmstrip-right.png'))

        # Vertical scrolling is actual window-routed touch and never selection.
        bar = strip.current_scrollbar()
        assert bar.maximum() > 0
        bar.setValue(0)
        settle()
        target = window.windowHandle()
        point = strip.viewport().mapTo(window, strip.viewport().rect().center())
        distance = min(90, strip.viewport().height() // 4)
        sequence = QTest.touchEvent(target, device, False)
        sequence.press(0, point + QPoint(0, distance), target).commit()
        sequence.move(0, point, target).commit()
        sequence.release(0, point - QPoint(0, distance), target).commit()
        settle()
        assert bar.value() > 0 and selected.count() == 0
        assert window._current_id == current_id
        click(panel.side_button)
        assert panel.side == 'left' and panel.hide_button.text() == 'X'
        assert [button.text() for button in (panel.hide_button, panel.orientation_button, panel.side_button)] == ['X', '底部', '右侧']
        assert strip.verticalScrollBar().mapTo(strip, QPoint()).x() < strip.viewport().x()
        left_before = state()
        drag(32)
        assert panel.width() > left_before['film_width'] and state()['viewer_width'] < left_before['viewer_width']
        drag(-32, touch=True)
        _wait(lambda: abs(panel.width() - left_before['film_width']) <= 2, 3)
        preserved()
        window.grab().save(str(report_dir / 'filmstrip-left.png'))

        # Keep the complete display-corner LIVE badge and film controls separate.
        assert window._displayed_item.is_live, 'Layout self-test needs a live photo'
        window._toggle_fullscreen()
        settle()
        badge = window.fullscreen_live_badge
        assert window._fullscreen_active and panel.isVisible() and badge.isVisible()
        assert not panel.library_label.isVisible() and not panel.analysis_label.isVisible()
        for button in (panel.hide_button, panel.orientation_button, panel.side_button):
            rect = QRect(button.mapTo(window.centralWidget(), QPoint()), button.size())
            assert not rect.intersects(badge.geometry()), 'Fullscreen LIVE badge covers a filmstrip control'
        window.grab().save(str(report_dir / 'filmstrip-left-fullscreen.png'))
        click(panel.side_button)
        assert panel.side == 'right' and panel.isVisible()
        click(panel.side_button)
        assert panel.side == 'left' and badge.isVisible()
        click(panel.hide_button)
        assert not panel.isVisible()
        window._toggle_strip(True)
        settle()
        assert panel.isVisible() and panel.side == 'left'
        window._toggle_fullscreen()
        settle()
        window._toggle_grid(True)
        settle()
        assert window._grid_mode and not panel.isVisible()
        window._toggle_grid(False)
        settle()
        assert panel.isVisible() and panel.side == 'left'

        click(panel.orientation_button)
        assert panel.orientation == 'horizontal' and panel.side == 'left'
        click(panel.orientation_button)
        assert panel.orientation == 'vertical' and panel.side == 'right'
        click(panel.orientation_button)
        click(panel.side_button)
        assert panel.orientation == 'vertical' and panel.side == 'left'
        window._set_film_width(480)
        click(panel.orientation_button)
        window._set_film_height(480)
        window.resize(940, 650)
        settle()
        small_height = panel.height()
        assert window.preferences.filmstrip_height == 480 and small_height < 480, {
            'window': window.size().toTuple(), 'photo_area': window.photo_area.height(),
            'film': small_height, 'preferred': window.preferences.filmstrip_height}
        window.resize(1500, 950)
        settle()
        assert window.preferences.filmstrip_height == 480 and panel.height() == 480
        click(panel.side_button)
        assert window.preferences.filmstrip_width == 480 and panel.width() == 480
        window._save_preferences()
        expected = {name: getattr(window.preferences, name) for name in fields}
        assert {name: getattr(Preferences.load(), name) for name in fields} == expected
        restarted = MainWindow()
        restarted.resize(1280, 850)
        restarted.show()
        settle()
        assert {name: getattr(restarted.preferences, name) for name in fields} == expected
        assert (restarted.film.orientation, restarted.film.side) == ('vertical', 'left')
        assert restarted.strip.scroll_axis == 'vertical'
        return {'bottom_left_right_buttons': True, 'close_button_and_three_direct_dock_controls': True,
                'bottom_direct_left_close_right_controls': True,
                'vertical_bottom_button_and_opposite_side_controls': True,
                'vertical_scrollbar_on_outer_side': True,
                'right_vertical_control_order': True,
                'mouse_touch_resize_both_sides': True, 'thumbnails_follow_thickness': True,
                'vertical_window_touch_scroll_without_selection': True,
                'same_model_and_cached_thumbnail_identity': True, 'browsing_anchor_preserved': True,
                'pair_analysis_settled_before_layout_measurement': True,
                'photo_transform_and_center_preserved': True, 'no_rescan_selection_or_playback': True,
                'fullscreen_left_badge_and_buttons_do_not_overlap': True,
                'fullscreen_and_overview_visibility': True, 'navigator_inside_remaining_viewport': True,
                'layout_side_and_dimensions_restart_restored': True,
                'small_window_keeps_preferred_dimensions': True, 'small_window_actual_height': small_height,
                'touch_target': 'QWindow', 'physical_touch_hardware': False,
                'remembered_layout': expected}
    finally:
        if trace_reset is not None:
            model.modelReset.disconnect(trace_reset)
        if restarted is not None:
            restarted.close()
            assert restarted.wait_for_shutdown(25)
            restarted.deleteLater()
        if window._fullscreen_active:
            window._toggle_fullscreen()
        window._toggle_grid(False)
        for name, value in original.items():
            setattr(window.preferences, name, value)
        window.restoreGeometry(geometry)
        window._set_film_layout(original['filmstrip_orientation'], original['filmstrip_side'], remember=False)
        window._clamp_film_height()
        window._toggle_strip(remembered)
        window._save_preferences()
        settle()
        if not viewer._image_rect.isEmpty():
            viewer.setTransform(original_transform)
            viewer.centerOn(original_center)
            viewer._fit_mode = original_fit
            viewer.zoom_changed.emit(viewer.zoom_factor())


def _check_reference_touch_map(window, fixtures: Path, report_dir: Path):
    """Exercise indirect gestures on the real detached app with QWindow input."""
    from unittest.mock import patch
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QMenu

    viewer = window.viewer
    original_id, original_grid = window._current_id, window._grid_mode
    original_hold = viewer._hold_ms
    player = viewer._player
    row = next(row for row, item in enumerate(window.model.items) if item.image_path.stem == 'Portrait_HEVC')
    portrait_id = window.model.items[row].id
    pinch_diagnostics = []

    def select_portrait():
        current_row = next(row for row, item in enumerate(window.model.items) if item.id == portrait_id)
        window._select_row(current_row, allow_autoplay=False)
        _wait(lambda: window._displayed_item is not None and window._displayed_item.id == portrait_id
              and window._current_full and not viewer._photo.pixmap().isNull(), 15)

    try:
        select_portrait()
        window._detach_preview()
        window._toggle_touch_map(True)
        surface = window.touch_map_panel.surface
        _wait(lambda: surface.isVisible() and surface.width() >= 80
              and window.preview_window.isVisible(), 5)
        window.activateWindow()
        window.grid.setFocus()
        QCoreApplication.processEvents()
        target = surface.window().windowHandle()
        assert target is not None and target.isExposed()
        assert window.preview_window.content.currentWidget() is viewer and viewer._player is player
        assert window.content.currentWidget() is window.grid
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(target, device, False)

        def point(position):
            return surface.mapTo(surface.window(), surface.map_from_viewer(QPointF(position)).toPoint())

        def view_geometry():
            displayed = viewer.viewportTransform().mapRect(viewer._image_rect)
            return {'viewport': [viewer.viewport().width(), viewer.viewport().height()],
                    'image_scene': list(viewer._image_rect.getRect()),
                    'displayed_image': list(displayed.getRect()),
                    'scale': viewer.transform().m11(), 'fit_scale': viewer._fit_scale,
                    'zoom': viewer.zoom_factor()}

        def pinch_case(name, anchor_in_viewport, *, require_two_axes):
            anchor = point(anchor_in_viewport)
            mapped = surface.map_to_viewer(QPointF(surface.mapFrom(surface.window(), anchor)))
            subject = viewer.mapToScene(mapped.toPoint())
            before = view_geometry()
            diagnostic = {'case': name, 'qpa': QApplication.platformName(),
                          'source_name': window._displayed_item.image_path.name,
                          'map_rect': list(surface.mapped_rect().getRect()),
                          'anchor_in_window': [anchor.x(), anchor.y()],
                          'mapped_anchor': [mapped.x(), mapped.y()],
                          'scene_before': [subject.x(), subject.y()], 'before': before}
            pinch_diagnostics.append(diagnostic)
            first, second = anchor - QPoint(20, 0), anchor + QPoint(20, 0)
            sequence.press(0, first, target).commit()
            sequence.stationary(0).press(1, second, target).commit()
            try:
                sequence.move(0, first - QPoint(20, 0), target).move(1, second + QPoint(20, 0), target).commit()
                after = view_geometry()
                actual = viewer.mapToScene(mapped.toPoint())
                delta = actual - subject
                displayed = viewer.viewportTransform().mapRect(viewer._image_rect)
                fitted = [axis for axis, size, available in (
                    ('x', displayed.width(), viewer.viewport().width()),
                    ('y', displayed.height(), viewer.viewport().height())) if size <= available]
                diagnostic.update(after=after, scene_after=[actual.x(), actual.y()],
                                  scene_error=[delta.x(), delta.y()], fitted_axes=fitted)
                assert abs(after['zoom'] - before['zoom'] * 2) < .05, diagnostic
                if require_two_axes:
                    assert not fitted, diagnostic
                    assert before['displayed_image'][2] > before['viewport'][0], diagnostic
                    assert before['displayed_image'][3] > before['viewport'][1], diagnostic
                    assert abs(mapped.x() - viewer._viewport_center().x()) > before['viewport'][0] * .05, diagnostic
                    assert abs(mapped.y() - viewer._viewport_center().y()) > before['viewport'][1] * .05, diagnostic
                    assert delta.manhattanLength() < 8, diagnostic
                else:
                    # QGraphicsView centers an axis whose entire image fits.
                    # Only a scrollable axis can keep an off-center subject.
                    if 'x' in fitted:
                        assert abs(displayed.center().x() - viewer.viewport().width() / 2) < 2, diagnostic
                    else:
                        assert abs(delta.x()) < 8, diagnostic
                    if 'y' in fitted:
                        assert abs(displayed.center().y() - viewer.viewport().height() / 2) < 2, diagnostic
                    else:
                        assert abs(delta.y()) < 8, diagnostic
            finally:
                sequence.release(0, first - QPoint(20, 0), target).release(1, second + QPoint(20, 0), target).commit()

        center = point(viewer.viewport().rect().center())
        area = surface.mapped_rect()
        assert abs(area.width() / area.height() - viewer.viewport().width() / viewer.viewport().height()) < 1e-6
        assert (surface.map_to_viewer(surface.map_from_viewer(QPointF(117, 93))) - QPointF(117, 93)).manhattanLength() < 1e-6
        revision = surface.thumbnail_revision
        assert max(surface._thumbnail.width(), surface._thumbnail.height()) <= surface.MAX_THUMBNAIL_EDGE
        with patch.object(player, 'play', wraps=player.play) as playback:
            sequence.press(0, center, target).commit()
            _wait(lambda: playback.call_count == 1, 5)
            assert QApplication.activeWindow() is window, 'Map activated the preview window'
            assert viewer._input_owner is surface and viewer._player is player
            sequence.release(0, center, target).commit()
            assert not viewer._play_requested

            # A fit swipe changes the actual main selection and detached photo.
            before_id = window._current_id
            sequence.press(0, center, target).commit()
            sequence.move(0, center + QPoint(-100, 0), target).commit()
            sequence.release(0, center + QPoint(-100, 0), target).commit()
            _wait(lambda: window._current_id != before_id and window._shown_image_path is not None, 12)
            select_portrait()
            viewer.fit_image()
            center = point(viewer.viewport().rect().center())
            viewport = viewer.viewport().size()
            pinch_case('fit_to_two_with_axis_clamping', viewer._viewport_center() + QPointF(
                viewport.width() * .05, -viewport.height() * .05), require_two_axes=False)
            viewer.fit_image()
            fill_scale = max(viewport.width() / viewer._image_rect.width(),
                             viewport.height() / viewer._image_rect.height())
            base_zoom = max(1.5, fill_scale * 1.25 / viewer._fit_scale)
            viewer._zoom_at(base_zoom, viewer._viewport_center())
            pinch_case('noncentral_two_axis_anchor', viewer._viewport_center() + QPointF(
                viewport.width() * .1, -viewport.height() * .1), require_two_axes=True)
            viewer.stop_live()
            scale, before = viewer.transform().m11(), viewer._scene_center()
            sequence.press(0, center, target).commit()
            sequence.release(0, center, target).commit()
            sequence.press(0, center + QPoint(2, 0), target).commit()
            QTest.qWait(original_hold + 80)
            assert playback.call_count == 1 and not viewer._hold_timer.isActive()
            sequence.move(0, center + QPoint(35, 15), target).commit()
            sequence.release(0, center + QPoint(35, 15), target).commit()
            assert abs(viewer.transform().m11() - scale) < 1e-6
            assert (viewer._scene_center() - before).manhattanLength() > .5

            # Reusing geometry never rebuilds the bounded reference photo.
            revision = surface.thumbnail_revision
            key = surface._thumbnail.cacheKey()
            for _ in range(10):
                viewer._pan_by(QPointF(3, -2))
            assert surface.thumbnail_revision == revision and surface._thumbnail.cacheKey() == key
            navigation = surface.navigator_rect()
            assert not navigation.isEmpty()
            navigation_point = surface.mapTo(surface.window(), (navigation.topLeft() + QPointF(3, 3)).toPoint())
            before = viewer._scene_center()
            sequence.press(0, navigation_point, target).commit()
            sequence.move(0, navigation_point + QPoint(6, 9), target).commit()
            sequence.release(0, navigation_point + QPoint(6, 9), target).commit()
            assert (viewer._scene_center() - before).manhattanLength() > .5
            assert playback.call_count == 1
            surface.grab().save(str(report_dir / 'reference-touch-map-zoomed.png'))

            # A second input surface cannot replace an active owner.
            sequence.press(0, center, target).commit()
            preview_target = window.preview_window.windowHandle()
            direct = QTest.touchEvent(preview_target, QTest.createTouchDevice(), False)
            direct_point = viewer.viewport().mapTo(window.preview_window, viewer.viewport().rect().center())
            direct.press(1, direct_point, preview_target).commit()
            direct.release(1, direct_point, preview_target).commit()
            assert viewer._input_owner is surface
            surface.cancel_interaction()
            sequence.release(0, center, target).commit()
            sequence.press(0, center, target).commit()
            other = next(index for index, item in enumerate(window.model.items) if item.id != portrait_id)
            window._select_row(other, allow_autoplay=False)
            sequence.release(0, center, target).commit()
            assert viewer._input_owner is None and not viewer._hold_timer.isActive()
            assert not viewer.is_zoom_animating()
            select_portrait()
            viewer.fit_image()
            QCoreApplication.processEvents()
            assert playback.call_count == 1

            # Use the application's real context menu and its nested event loop.
            window.activateWindow()
            window.grid.setFocus()
            QCoreApplication.processEvents()
            blank = point(QPoint(3, viewer.viewport().height() // 2))
            assert not surface.image_contains_viewport_point(QPointF(surface.mapFrom(surface.window(), blank)))
            global_blank = surface.window().mapToGlobal(blank)
            menu_result, errors = {}, []
            requested_menu = QSignalSpy(surface.contextMenuRequested)

            def release_opening_contact():
                popup = QApplication.activePopupWidget()
                try:
                    assert isinstance(popup, QMenu), 'Blank hold did not open the real image menu'
                    menu_result['popup_geometry'] = list(popup.geometry().getRect())
                    popup.grab().save(str(report_dir / 'reference-touch-map-blank-menu.png'))
                    sequence.release(0, blank, target).commit()
                    menu_result['kept_open'] = popup.isVisible()
                except Exception as error:
                    errors.append(error)
                finally:
                    menu_result['released'] = True
                    if popup is not None:
                        QTimer.singleShot(40, popup.close)

            QTest.mouseMove(surface.window(), surface.mapTo(surface.window(), QPoint(1, 1)))
            QTimer.singleShot(QApplication.styleHints().mousePressAndHoldInterval() + 200, release_opening_contact)
            sequence.press(0, blank, target).commit()
            _wait(lambda: menu_result.get('released'), 5)
            assert not errors, repr(errors)
            # Qt may clamp a popup onto the available screen (the offscreen
            # test screen can be smaller than the main window). The requested
            # location must still be the actual map contact, never preview XY.
            assert requested_menu.count() == 1 and requested_menu.at(0)[0] == global_blank
            assert menu_result['kept_open'] and playback.call_count == 1
        surface.grab().save(str(report_dir / 'reference-touch-map-fit.png'))
        return {'same_viewer_and_player': True, 'reference_matches_viewport': True,
                'normalized_logical_mapping': True, 'bounded_thumbnail_edge': surface.MAX_THUMBNAIL_EDGE,
                'cache_reused_without_io': True, 'hold_release_live': True,
                'map_does_not_activate_preview': True, 'fit_swipe_and_zoom_pan': True,
                'pinch_anchor': True, 'second_contact_no_live': True,
                'pinch_geometry_cases': pinch_diagnostics,
                'added_finger_cancels_hold': True,
                'second_contact_pause_ms': original_hold + 80, 'hold_delay_ms': original_hold,
                'blank_menu_at_map_finger': True, 'menu_release_keeps_popup': True,
                'navigator_reachable': True, 'one_owner': True,
                'source_change_discards_late_release': True, 'touch_target': 'QWindow',
                'physical_touch_hardware': False}
    finally:
        (report_dir / 'reference-map-pinch-diagnostics.json').write_text(
            json.dumps(pinch_diagnostics, ensure_ascii=False, indent=2), encoding='utf-8')
        viewer.cancel_interaction()
        viewer.set_options(window.preferences.loop, window.preferences.muted,
                           preserve_zoom=window.preferences.preserve_zoom, hold_ms=original_hold,
                           horizontal_scroll_mode=window.preferences.horizontal_scroll_mode)
        if window._detached:
            window._redock_preview()
        restore = next((index for index, item in enumerate(window.model.items) if item.id == original_id), None)
        if restore is not None:
            window._select_row(restore, allow_autoplay=False)
        window._toggle_grid(original_grid)


def _check_fullscreen_edge_gestures(window, report_dir: Path):
    from unittest.mock import patch
    from PySide6.QtTest import QSignalSpy

    gesture = window.fullscreen_edge_gesture
    native = gesture.native_policy
    native_verified = native_cleanup = False
    original_edges, original_grid = gesture.enabled_edges, window._grid_mode
    selected = window._current_id
    saved = {key: getattr(window.preferences, key) for key in (
        'map_floating', 'map_dock_edge', 'map_dock_width', 'map_bottom_height',
        'preview_geometry', 'preview_screen', 'map_float_geometry', 'map_float_screen')}
    assert original_edges == frozenset(window.preferences.fullscreen_exit_gestures)
    assert not window._detached, 'Run the edge self-check after restoring the main photo'
    device = QTest.createTouchDevice()
    clicked = QSignalSpy(window.grid.clicked)
    navigated = QSignalSpy(window.viewer.navigate_requested)
    requests = QSignalSpy(gesture.escapeRequested)
    routes = []
    side_inset_routes = []
    native_checks = []

    def native_snapshot(target_window, stage, hwnd=None, cookie=None, *, read_value=True):
        record = native._tracked.get(id(target_window))
        snapshot = {'stage': stage, 'target': 'main' if target_window is window else 'preview',
                    'hwnd': hwnd, 'cookie': cookie, 'policy': native.report(),
                    'visible': target_window.isVisible(), 'fullscreen': target_window.isFullScreen(),
                    'active': target_window.isActiveWindow(),
                    'record': {key: record.get(key) for key in ('hwnd', 'cookie', 'set')} if record else None}
        native_checks.append(snapshot)
        if native.supported and hwnd and native._backend is not None:
            try:
                snapshot['own_window'] = bool(native._backend.is_own_window(hwnd))
                snapshot['cookie_matches'] = bool(native._backend.matches(hwnd, cookie)) if cookie else False
                snapshot['actual_cookie'] = native._backend.user.GetPropW(
                    hwnd, 'LiveViewer.FullscreenEdgeGesture.Owner')
                if read_value:
                    snapshot['actual'] = native._backend.read(hwnd)
            except Exception as error:
                snapshot['read_error'] = f'{type(error).__name__}: {error}'
            finally:
                if read_value:
                    snapshot['readback'] = dict(getattr(native._backend, 'last_readback', None) or {})
        return snapshot

    def save_native_diagnostics():
        (report_dir / 'fullscreen-edge-native-diagnostics.json').write_text(
            json.dumps({'checks': native_checks, 'routes': routes, 'side_inset_routes': side_inset_routes,
                        'policy': native.report()},
                       ensure_ascii=False, indent=2), encoding='utf-8')

    def require_native_value(target_window, stage, hwnd, expected, cookie=None):
        snapshot = native_snapshot(target_window, stage, hwnd, cookie)
        save_native_diagnostics()
        assert snapshot.get('actual') == expected, json.dumps(snapshot, ensure_ascii=False)
        readback = snapshot['readback']
        if expected['vt'] == 11:
            assert readback.get('raw_vt') in (11, 31) and readback.get('conversion_succeeded') is True, snapshot
            assert readback.get('canonical_vt') == 11 and readback.get('value') is expected['value'], snapshot
        else:
            assert readback.get('raw_vt') == 0 and readback.get('value') is None, snapshot
        return snapshot

    def swipe(target_window, edge, *, corner=False, inset=4, side_case=False):
        nonlocal native_verified, native_cleanup
        QCoreApplication.processEvents()
        target = target_window.windowHandle()
        _wait(lambda: target.isExposed() and target_window.isFullScreen(), 5)
        native.sync_now()
        hwnd, cookie = None, None
        if native.supported:
            hwnd = int(target_window.winId())
            record = native._tracked.get(id(target_window))
            snapshot = native_snapshot(target_window, 'before_swipe_record', hwnd, read_value=False)
            save_native_diagnostics()
            assert record is not None, json.dumps(snapshot, ensure_ascii=False)
            cookie = record['cookie']
            snapshot = require_native_value(target_window, 'before_swipe_true', hwnd,
                                            {'vt': 11, 'value': True}, cookie)
            assert record['hwnd'] == hwnd and snapshot['cookie_matches'], json.dumps(snapshot, ensure_ascii=False)
            native_verified = True
        starts = {'top': QPoint(target_window.width() // 2, inset),
                  'left': QPoint(inset, target_window.height() // 2),
                  'right': QPoint(target_window.width() - inset, target_window.height() // 2)}
        start = QPoint(4, 4) if corner else starts[edge]
        delta = {'top': QPoint(2, 110), 'left': QPoint(110, 2), 'right': QPoint(-110, 2)}[edge]
        sequence = QTest.touchEvent(target, device, False)
        count = requests.count()
        navigation_before, playback_before = navigated.count(), playback.call_count
        sequence.press(0, start, target).commit()
        assert not window.viewer._hold_timer.isActive()
        if side_case:
            QTest.qWait(window.viewer._hold_ms + 20)
        sequence.move(0, start + delta, target).commit()
        _wait(lambda: not target_window.isFullScreen(), 5)
        native.sync_now()
        if native.supported:
            snapshot = require_native_value(target_window, 'after_escape_empty', hwnd,
                                            {'vt': 0, 'value': None}, cookie)
            assert not snapshot['cookie_matches'], json.dumps(snapshot, ensure_ascii=False)
            native_cleanup = True
        sequence.release(0, start + delta, target).commit()
        QCoreApplication.processEvents()
        assert requests.count() == count + 1 and requests.at(count)[0] is target_window
        assert window._current_id == selected and not window.viewer._play_requested
        assert navigated.count() == navigation_before and playback.call_count == playback_before
        result = {'edge': edge, 'corner': corner, 'escaped_once': True,
                  'target': 'main' if target_window is window else 'preview'}
        if side_case:
            result.update(inset=inset, no_navigation_or_live=True)
            side_inset_routes.append(result)
        else:
            routes.append(result)

    try:
        with patch.object(window.viewer._player, 'play', wraps=window.viewer._player.play) as playback:
            gesture.set_enabled_edges({'left'})
            window._toggle_grid(True)
            window._toggle_fullscreen()
            swipe(window, 'left')
            assert window._grid_mode and window.content.currentWidget() is window.grid
            window._toggle_grid(False)
            for edge in ('left', 'right'):
                gesture.set_enabled_edges({edge})
                for inset in (24, 36, 48):
                    window._toggle_fullscreen()
                    swipe(window, edge, inset=inset, side_case=True)
            window._detach_preview()
            window._toggle_touch_map(True)
            window._float_touch_map()
            preview = window.preview_window
            for edge, corner in (('top', False), ('left', False), ('right', False),
                                  ('top', True), ('left', True)):
                gesture.set_enabled_edges({'top', 'left'} if corner else {edge})
                window._toggle_fullscreen()
                swipe(preview, edge, corner=corner)
                assert window._detached and window.preview_window is preview
                assert window.touch_map_window.isVisible() and window.touch_map_panel.isVisible()
            for edge in ('left', 'right'):
                gesture.set_enabled_edges({edge})
                for inset in (24, 36, 48):
                    window._toggle_fullscreen()
                    swipe(preview, edge, inset=inset, side_case=True)
                    assert window._detached and preview.isMaximized()
                    assert window.touch_map_window.isVisible() and window.touch_map_panel.isVisible()

            # Explicit empty configuration must not reserve a new contact.
            gesture.set_enabled_edges([])
            window._toggle_fullscreen()
            QCoreApplication.processEvents()
            target = preview.windowHandle()
            point = QPoint(preview.width() // 2, 4)
            sequence = QTest.touchEvent(target, device, False)
            count = requests.count()
            sequence.press(0, point, target).commit()
            assert window.viewer._touch_active
            sequence.move(0, point + QPoint(0, 50), target).commit()
            sequence.release(0, point + QPoint(0, 50), target).commit()
            assert preview.isFullScreen() and requests.count() == count
            native.sync_now()
            if native.supported:
                require_native_value(preview, 'explicit_empty', int(preview.winId()),
                                     {'vt': 0, 'value': None})

            gesture.set_enabled_edges({'left'})
            point = QPoint(4, preview.height() // 2)
            sequence.press(0, point, target).commit()
            gesture.set_enabled_edges([])
            sequence.move(0, point + QPoint(110, 0), target).commit()
            sequence.release(0, point + QPoint(110, 0), target).commit()
            assert preview.isFullScreen() and requests.count() == count
            assert not window.viewer._touch_active and not window.viewer._hold_timer.isActive()
            assert playback.call_count == 0 and clicked.count() == 0
            preview.grab().save(str(report_dir / 'fullscreen-edge-preview.png'))
            window._toggle_fullscreen()
        if native.supported:
            assert native_verified and native_cleanup and not native.report()['failures'], native.report()
        assert len(side_inset_routes) == 12 and gesture.side_width == 48 and gesture.edge_height == 20
        return {'routes': routes, 'side_inset_routes': side_inset_routes,
                'side_edge_width': gesture.side_width, 'top_edge_height': gesture.edge_height,
                'settings_edges_applied': True, 'all_three_directions': True,
                'corner_uses_actual_direction': True, 'explicit_empty_disables_reservation': True,
                'settings_change_cancels_pending': True, 'same_escape_path': True,
                'overview_stays_overview': True, 'preview_stays_detached_and_map_open': True,
                'release_does_not_activate_content': True, 'no_live_or_selection_change': True,
                'native_window_edge_policy': native.report(),
                'native_window_property_verified': native_verified,
                'native_window_cleanup_verified': native_cleanup,
                'native_window_boolean_readback_verified': native_verified,
                'touch_target': 'QWindow', 'physical_touch_hardware': False,
                'os_reserved_gesture_delivery_verified': False}
    finally:
        save_native_diagnostics()  # Preserve the failing native state before cleanup changes it.
        gesture.set_enabled_edges(original_edges)
        if window._fullscreen_active:
            window._exit_fullscreen()
        if window._detached:
            if window.preview_window.isFullScreen():
                window.preview_window.toggle_fullscreen()
            window._redock_preview()
        for key, value in saved.items():
            setattr(window.preferences, key, value)
        window._toggle_grid(original_grid)
        window._save_preferences()


def _check_settings_controls(settings, window, report_dir: Path):
    assert '未放大时，水平滚轮始终切换上一张／下一张' in settings.horizontal_scroll_help.text()
    """Send real wheel/key/touch events through the shipped settings dialog."""
    from PySide6.QtGui import QAccessible
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QCheckBox
    from .settings_controls import PillSwitch

    scroll = settings.findChild(QScrollArea)
    bar = scroll.verticalScrollBar()
    settings.resize(settings.width(), min(settings.height(), 650))
    settings.show()
    settings.activateWindow()
    QCoreApplication.processEvents()
    assert bar.maximum() > 0, 'Settings fixture must have a scrollable page'

    def wheel(target, pixel, angle):
        position = target.rect().center()
        event = QWheelEvent(QPointF(position), QPointF(target.mapToGlobal(position)),
                            pixel, angle, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QCoreApplication.sendEvent(target, event)
        QCoreApplication.processEvents()

    wheel_events = 0
    targets = [(combo, combo.currentIndex) for combo in
               (settings.theme_mode, settings.mode, settings.zoom_mode,
                settings.horizontal_scroll_mode, settings.cache_location)]
    targets.append((settings.fullscreen_exit_gestures,
                    lambda: tuple(settings.fullscreen_exit_gestures.selected_gestures())))
    targets.extend((control, control.value) for control in (settings.hold, settings.cache))
    targets.extend((control.lineEdit(), control.value) for control in (settings.hold, settings.cache))
    for target, value in targets:
        for pixel_only in (False, True):
            scroll.ensureWidgetVisible(target)
            target.setFocus()
            QCoreApplication.processEvents()
            start, before = bar.value(), value()
            direction = -1 if start < bar.maximum() - 40 else 1
            pixel = QPoint(0, 27 * direction) if pixel_only else QPoint()
            angle = QPoint() if pixel_only else QPoint(0, 120 * direction)
            wheel(scroll.viewport(), pixel, angle)
            expected = bar.value()
            assert expected != start, 'Wheel did not scroll the settings page'
            bar.setValue(start)
            wheel(target, pixel, angle)
            assert value() == before, 'Wheel changed a closed settings control'
            assert bar.value() == expected, 'Settings wheel was lost or scrolled the page more than once'
            wheel_events += 1

    combo = settings.mode
    old_index = combo.currentIndex()
    scroll.ensureWidgetVisible(combo)
    combo.setCurrentIndex(0)
    combo.showPopup()
    QCoreApplication.processEvents()
    assert combo.view().isVisible(), 'Explicit settings dropdown did not open'
    QTest.keyClick(combo.view(), Qt.Key.Key_Down)
    QTest.keyClick(combo.view(), Qt.Key.Key_Return)
    QCoreApplication.processEvents()
    assert combo.currentIndex() == 1, 'Explicit dropdown selection stopped working'
    combo.setCurrentIndex(old_index)

    switches = (settings.autoplay, settings.muted, settings.override_keys, settings.recursive, settings.portable)
    for control in switches:
        assert isinstance(control, PillSwitch) and isinstance(control, QCheckBox)
        assert control.focusPolicy() != Qt.FocusPolicy.NoFocus
        accessible = QAccessible.queryAccessibleInterface(control)
        assert accessible is not None and accessible.role() == QAccessible.Role.CheckBox
        assert bool(accessible.state().checked) == control.isChecked()
    assert all(isinstance(control, QCheckBox) and not isinstance(control, PillSwitch)
               for control in (window.kind_filter.live, window.kind_filter.static, window.kind_filter.pending))

    control = settings.autoplay
    before = control.isChecked()
    scroll.ensureWidgetVisible(control)
    control.setFocus()
    QCoreApplication.processEvents()
    position = QPoint(20, control.height() // 2)
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=position)
    assert control.isChecked() != before, 'Pill switch did not respond to a click'
    QTest.keyClick(control, Qt.Key.Key_Space)
    assert control.isChecked() == before, 'Pill switch did not respond to Space'
    QCoreApplication.processEvents()
    target_window = settings.windowHandle()
    assert target_window is not None and target_window.isExposed(), 'Settings window must be exposed for touch injection'
    window_position = control.mapTo(settings, position)
    assert settings.rect().contains(window_position), 'Mapped touch point is outside the settings window'
    touch_toggled, touch_clicked = QSignalSpy(control.toggled), QSignalSpy(control.clicked)
    device = QTest.createTouchDevice()
    touch = QTest.touchEvent(target_window, device, False)
    touch.press(0, window_position, target_window).commit()
    touch.release(0, window_position, target_window).commit()
    QCoreApplication.processEvents()
    assert control.isChecked() != before, 'Pill switch did not respond to simulated touch'
    touch_toggle_count, touch_click_count = touch_toggled.count(), touch_clicked.count()
    assert touch_toggle_count == 1 and touch_click_count == 1, 'A touch must toggle and click the switch exactly once'
    control.setChecked(before)
    control.setEnabled(False)
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=position)
    QTest.keyClick(control, Qt.Key.Key_Space)
    assert control.isChecked() == before, 'Disabled pill switch changed its value'
    control.setEnabled(True)
    bar.setValue(0)
    QCoreApplication.processEvents()
    settings.grab().save(str(report_dir / 'settings-switches.png'))
    return {'closed_control_wheel_events': wheel_events,
            'fullscreen_exit_gesture_selector_wheel_protected': True,
            'horizontal_scroll_setting_label': '放大时水平滚轮',
            'focused_controls_and_spinbox_children_protected': True,
            'angle_and_pixel_scroll_page_once': True, 'opened_dropdown_selectable': True,
            'checkbox_accessibility_preserved': True, 'click_space_touch_toggle': True,
            'touch_target': 'settings_qwindow', 'touch_toggle_count': touch_toggle_count,
            'touch_click_count': touch_click_count,
            'touch_window_position': [window_position.x(), window_position.y()],
            'disabled_switch_unchanged': True, 'overview_filter_remains_checkboxes': True}


def _check_application_theme(window, report_dir: Path):
    from .app import SettingsDialog
    from .settings import Preferences

    app = QApplication.instance()
    controller = window.theme_controller
    original_mode = window.preferences.theme_mode
    original_provider = controller.system_provider
    original_grid_mode = window._grid_mode
    provider_state = ['dark']
    controller.system_provider = lambda: provider_state[0]
    window._toggle_grid(False)
    _wait(lambda: not window.viewer._photo.pixmap().isNull(), 8)
    _wait(lambda: all(not item.pending for item in window._primary_items), 20)
    window.viewer.fit_image()
    window.viewer.zoom_in()
    window.viewer.zoom_in()
    QCoreApplication.processEvents()
    window.viewer._pan_by(QPointF(20, -15))
    QTest.qWait(80)

    def image_digest():
        image = window.viewer._photo.pixmap().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        return hashlib.sha256(image.constBits()).hexdigest()

    epoch, selected = window._epoch, window._current_id
    scale, center = window.viewer.transform().m11(), window.viewer._scene_center()
    before_geometry = {'scale': scale, 'fit_scale': window.viewer._fit_scale,
                       'viewport': [window.viewer.viewport().width(), window.viewer.viewport().height()],
                       'center': [center.x(), center.y()]}
    image_before = image_digest()
    grid_width = window.grid.thumbnail_width
    resets = []
    def record_reset():
        resets.append(True)
    window.model.modelReset.connect(record_reset)

    def save_mode(mode, accept=True):
        errors = []
        def choose():
            dialog = app.activeModalWidget()
            try:
                assert isinstance(dialog, SettingsDialog)
                assert [dialog.theme_mode.itemData(index) for index in range(dialog.theme_mode.count())] == [
                    'dark', 'light', 'system']
                dialog.theme_mode.setCurrentIndex(dialog.theme_mode.findData(mode))
                dialog._accept() if accept else dialog.reject()
            except Exception as error:
                errors.append(error)
                if dialog is not None:
                    dialog.reject()
        QTimer.singleShot(0, choose)
        window.open_settings()
        if errors:
            raise errors[0]
        QCoreApplication.processEvents()

    def background(widget):
        return widget.grab().toImage().pixelColor(3, 3).lightness()

    dialog = None
    menu = window.kind_filter.menu
    try:
        save_mode('dark')
        assert controller.mode == 'dark' and controller.effective_theme == 'dark'
        assert background(window.header) < 100
        save_mode('light')
        assert controller.mode == 'light' and controller.effective_theme == 'light'
        assert background(window.header) > 180, 'Light mode did not change the rendered window'
        assert Preferences.load().theme_mode == 'light'
        restored = SettingsDialog(Preferences.load(), window)
        assert restored.theme_mode.currentData() == 'light'
        restored.close()
        window.grab().save(str(report_dir / 'theme-light-window.png'))

        save_mode('system')
        assert controller.mode == 'system' and controller.effective_theme == 'dark'
        dialog = SettingsDialog(window.preferences, window)
        dialog.show()
        QCoreApplication.processEvents()
        assert background(dialog) < 100
        provider_state[0] = 'light'
        controller.refresh_system()
        QCoreApplication.processEvents()
        assert controller.effective_theme == 'light' and background(dialog) > 180
        assert dialog.isVisible() and dialog.theme_mode.currentData() == 'system'
        dialog.grab().save(str(report_dir / 'theme-light-settings.png'))
        dialog.close()
        dialog = None
        menu.popup(window.kind_filter.mapToGlobal(QPoint(0, window.kind_filter.height())))
        QCoreApplication.processEvents()
        assert menu.isVisible() and background(menu) > 180
        provider_state[0] = 'dark'
        controller.refresh_system()
        QCoreApplication.processEvents()
        assert menu.isVisible() and background(menu) < 100
        provider_state[0] = 'light'
        controller.refresh_system()
        QCoreApplication.processEvents()
        assert menu.isVisible() and background(menu) > 180
        menu.grab().save(str(report_dir / 'theme-light-filter.png'))
        menu.hide()
        assert Preferences.load().theme_mode == 'system'
        save_mode('dark', accept=False)
        assert window.preferences.theme_mode == 'system' and controller.mode == 'system'
        assert Preferences.load().theme_mode == 'system'
        assert window._epoch == epoch and not resets, 'Changing appearance rescanned or reset the gallery'
        assert window._current_id == selected and window.grid.thumbnail_width == grid_width
        after_center = window.viewer._scene_center()
        after_geometry = {'scale': window.viewer.transform().m11(), 'fit_scale': window.viewer._fit_scale,
                          'viewport': [window.viewer.viewport().width(), window.viewer.viewport().height()],
                          'center': [after_center.x(), after_center.y()]}
        assert abs(after_geometry['scale'] - scale) < .00001, (before_geometry, after_geometry)
        assert (window.viewer._scene_center() - center).manhattanLength() < 1.5
        assert image_digest() == image_before, 'Theme changed the displayed image pixels'
        return {'three_modes_and_saved_restore': True, 'cancel_preserves_saved_mode': True,
                'rendered_light_palette_verified': True, 'system_changes_update_open_settings_and_menu': True,
                'selection_zoom_and_gallery_preserved': True, 'image_pixels_unchanged': True,
                'view_geometry_before': before_geometry, 'view_geometry_after': after_geometry,
                'zoom_basis': 'actual_image_transform',
                'system_change_source': 'injected_provider', 'windows_registry_modified': False,
                'powertoys_started': False}
    finally:
        if dialog is not None:
            dialog.close()
        menu.hide()
        window.model.modelReset.disconnect(record_reset)
        controller.system_provider = original_provider
        save_mode(original_mode)
        window.viewer.fit_image()
        window._toggle_grid(original_grid_mode)


def _check_pointer_contact_lifecycle(fixtures: Path):
    """Inject complete contacts, including Qt Windows' between-contact Leave.

    This exercises the real canvas and QWindow routing; the native mouse
    evidence fixtures below are simulated and do not establish hardware support.
    """
    from unittest.mock import patch
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent, QPointingDevice
    from .native_pointer_input import PointerEvidence

    viewer = ViewerCanvas()
    viewer.resize(800, 600)
    viewer.show()
    viewer.activateWindow()
    viewer.setFocus()
    _wait(lambda: viewer.windowHandle() is not None and viewer.windowHandle().isExposed())
    target = viewer.windowHandle()
    device = QTest.createTouchDevice()
    sequence = QTest.touchEvent(target, device, False)
    image = QImage(2400, 1800, QImage.Format.Format_RGB32)
    image.fill('#5794b6')
    hold, pause = 100, 180
    center = viewer.viewport().rect().center()
    original_monitor = viewer._pointer_input_monitor
    leaves = 0

    class EvidenceFixture:
        def __init__(self, evidence):
            self.evidence = evidence

        def mouse_evidence(self, _event):
            return self.evidence

    def reset(zoom=1.0):
        viewer.set_photo(image, fixtures / 'Live_H264.MOV')
        viewer.set_options(hold_ms=hold)
        viewer._pointer_input_monitor = EvidenceFixture(PointerEvidence())
        if zoom != 1:
            viewer._zoom_at(zoom, QPointF(center))
        # Keep the native cursor inside the actual exposed input window so an
        # injected event cannot be cancelled by an unrelated cursor enter/leave.
        QTest.mouseMove(viewer.viewport(), center)
        QCoreApplication.processEvents()

    def point(local):
        return target.mapFromGlobal(viewer.viewport().mapToGlobal(local))

    def leave_between_contacts():
        nonlocal leaves
        assert not viewer._touch_active and not viewer._mouse_down
        # QWidget Leave belongs to the outer view. Sending it only to the
        # viewport does not call QGraphicsView.leaveEvent and misses this bug.
        QApplication.sendEvent(viewer, QEvent(QEvent.Type.Leave))
        leaves += 1

    def mouse(kind, local, timestamp, source=Qt.MouseEventSource.MouseEventNotSynthesized):
        moving, released = kind == QEvent.Type.MouseMove, kind == QEvent.Type.MouseButtonRelease
        event = QMouseEvent(kind, QPointF(local), QPointF(local),
                            QPointF(viewer.viewport().mapToGlobal(local)),
                            Qt.MouseButton.NoButton if moving else Qt.MouseButton.LeftButton,
                            Qt.MouseButton.NoButton if released else Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier, source,
                            QPointingDevice.primaryPointingDevice())
        event.setTimestamp(timestamp)
        QApplication.sendEvent(viewer.viewport(), event)
        QCoreApplication.processEvents()

    try:
        with patch.object(viewer._player, 'play', wraps=viewer._player.play) as playback:
            reset()
            for factor in (2.0, 1.0):
                first, second = point(center), point(center + QPoint(12, 4))
                sequence.press(0, first, target).commit()
                sequence.release(0, first, target).commit()
                leave_between_contacts()
                sequence.press(0, second, target).commit()
                QTest.qWait(pause)
                assert playback.call_count == 0 and not viewer._play_requested, 'Second touchscreen contact requested video before release'
                sequence.release(0, second, target).commit()
                _wait(lambda: not viewer.is_zoom_animating(), 3)
                assert abs(viewer.zoom_factor() - factor) < .005, 'Between-contact Leave broke touchscreen double-tap zoom'

            reset(2.0)
            scale, origin = viewer.transform().m11(), viewer._scene_center()
            first, second = point(center), point(center + QPoint(12, 4))
            sequence.press(0, first, target).commit()
            sequence.release(0, first, target).commit()
            leave_between_contacts()
            sequence.press(0, second, target).commit()
            QTest.qWait(pause)
            assert playback.call_count == 0 and not viewer._play_requested
            # Qt-promoted events from the same touchscreen contact must not
            # create a second hold, zoom or ownership change.
            mouse(QEvent.Type.MouseButtonDblClick, center + QPoint(12, 4), 1100,
                  Qt.MouseEventSource.MouseEventSynthesizedByQt)
            mouse(QEvent.Type.MouseButtonRelease, center + QPoint(12, 4), 1120,
                  Qt.MouseEventSource.MouseEventSynthesizedByQt)
            assert viewer._touch_active, 'Promoted mouse release cancelled the owning touch contact'
            sequence.move(0, second + QPoint(80, 35), target).commit()
            sequence.release(0, second + QPoint(80, 35), target).commit()
            assert playback.call_count == 0 and not viewer._play_requested
            assert abs(viewer.transform().m11() - scale) < 1e-6
            assert (viewer._scene_center() - origin).manhattanLength() > .5
            assert not viewer.is_zoom_animating(), 'Promoted mouse duplicate toggled zoom after touch drag'

            reset(2.0)
            scale, origin = viewer.transform().m11(), viewer._scene_center()
            hints = QApplication.styleHints()
            with patch.object(hints, 'mouseDoubleClickDistance', return_value=4), \
                    patch.object(hints, 'touchDoubleTapDistance', return_value=40):
                mouse(QEvent.Type.MouseButtonPress, center, 2000)
                mouse(QEvent.Type.MouseButtonRelease, center, 2020)
                leave_between_contacts()
                second = center + QPoint(12, 4)
                mouse(QEvent.Type.MouseButtonPress, second, 2100)
                QTest.qWait(pause)
                assert playback.call_count == 0 and not viewer._play_requested, 'Legacy unknown mouse route briefly played video during second-tap pause'
                mouse(QEvent.Type.MouseMove, second + QPoint(80, 35), 2300)
                mouse(QEvent.Type.MouseButtonRelease, second + QPoint(80, 35), 2320)
            assert abs(viewer.transform().m11() - scale) < 1e-6
            assert (viewer._scene_center() - origin).manhattanLength() > .5
            assert not viewer.is_zoom_animating()

            reset()
            # Immediately following touch input, a separate physical mouse
            # fixture still owns an ordinary long press at the configured delay.
            first = point(center)
            sequence.press(0, first, target).commit()
            sequence.release(0, first, target).commit()
            viewer._pointer_input_monitor = EvidenceFixture(PointerEvidence('mouse', True))
            mouse(QEvent.Type.MouseButtonPress, center, 3000)
            _wait(lambda: playback.call_count == 1, 3)
            assert viewer._hold_timer.interval() == hold and viewer._play_requested
            mouse(QEvent.Type.MouseButtonRelease, center, 3200)
            assert not viewer._play_requested

            reset()
            first = point(center)
            sequence.press(0, first, target).commit()
            _wait(lambda: playback.call_count == 2, 3)
            sequence.release(0, first, target).commit()
            assert not viewer._play_requested
        return {'touch_target': 'QWindow', 'touch_double_tap_fit_twice_fit_after_leave': True,
                'touch_second_pause_then_drag_after_leave': True,
                'unknown_mouse_legacy_second_pause_then_drag_after_leave': True,
                'leave_events_between_contacts': leaves, 'leave_receiver': 'ViewerCanvas',
                'second_contact_pause_ms': pause, 'hold_delay_ms': hold,
                'no_transient_video_request': True, 'drag_preserves_zoom_and_pans': True,
                'promoted_mouse_deduplicated_without_cancelling_touch': True,
                'independent_mouse_after_touch_and_touch_hold_play': True,
                'hold_requests': 2, 'mouse_evidence': 'simulated unknown and positive physical mouse',
                'physical_touch_hardware': False, 'physical_touchpad_hardware': False}
    finally:
        viewer._pointer_input_monitor = original_monitor
        viewer.stop_live()
        viewer.close()
        viewer.deleteLater()
        QCoreApplication.processEvents()


def _check_pointer_input_capability(viewer: ViewerCanvas):
    """Observe available native APIs/devices without asserting PTP hardware."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent, QPointingDevice
    from .native_pointer_input import PointerEvidence, install_pointer_input_monitor

    monitor = viewer._pointer_input_monitor or install_pointer_input_monitor(viewer)
    if monitor is None:
        diagnostics = {'platform': sys.platform, 'raw_input': 'unavailable',
                       'physical_button_gating_requires_explicit_touchpad_source': True,
                       'preview_touchpad_registration_enabled': False}
    else:
        diagnostics = monitor.diagnostics()
        assert diagnostics['raw_input'] in ('registered', 'existing-registration', 'unavailable')
        for key in ('ptp_devices', 'button_capable_devices', 'raw_reports_observed',
                    'explicit_touchpad_messages', 'matched_native_messages', 'observation_errors'):
            assert isinstance(diagnostics[key], int) and diagnostics[key] >= 0, key
        assert diagnostics['button_capable_devices'] <= diagnostics['ptp_devices']
        local = viewer.viewport().rect().center()
        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(local), QPointF(local),
                            QPointF(viewer.viewport().mapToGlobal(local)),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventNotSynthesized,
                            QPointingDevice.primaryPointingDevice())
        # No corresponding native message exists for this unique timestamp.
        event.setTimestamp(0x76543210)
        assert monitor.mouse_evidence(event) == PointerEvidence(), 'Uncorrelated Qt event received physical button evidence'
    if sys.platform == 'win32' and QApplication.platformName() == 'windows':
        assert monitor is not None, 'Windows native pointer observer failed to initialize'
        assert diagnostics['raw_input'] in ('registered', 'existing-registration'), diagnostics
        assert diagnostics['observation_errors'] == 0, diagnostics
    assert diagnostics['physical_button_gating_requires_explicit_touchpad_source'] is True
    assert diagnostics['preview_touchpad_registration_enabled'] is False
    json.dumps(diagnostics)
    return {'native_observer_installed': monitor is not None, 'diagnostics': diagnostics,
            'uncorrelated_input_remains_unknown': True,
            'physical_button_state_verified': False, 'physical_touchpad_hardware': False,
            'physical_touch_hardware': False,
            'scope': 'native API capability observation; gesture events are separately injected'}


def _check_touch_scroll_and_double_tap(report_dir: Path):
    """Exercise native QWindow delivery through settings children and canvas."""
    from unittest.mock import patch
    from .app import SettingsDialog, STYLE
    from .settings import Preferences

    settings = SettingsDialog(Preferences())
    settings.setStyleSheet(STYLE)
    settings.resize(680, 480)
    viewer = ViewerCanvas()
    routes = []
    try:
        settings.show()
        settings.activateWindow()
        _wait(lambda: settings.windowHandle() is not None and settings.windowHandle().isExposed())
        scroll, target = settings.scroll_area, settings.windowHandle()
        bar = scroll.verticalScrollBar()

        def touch_control(control, delta=QPoint()):
            settings.activateWindow()
            QTest.qWait(20)
            scroll.ensureWidgetVisible(control)
            QCoreApplication.processEvents()
            point = target.mapFromGlobal(control.mapToGlobal(control.rect().center()))
            assert scroll.viewport().rect().contains(control.mapTo(scroll.viewport(), control.rect().center()))
            sequence = QTest.touchEvent(target, QTest.createTouchDevice(), False)
            sequence.press(0, point, target).commit()
            if not delta.isNull():
                sequence.move(0, point + delta, target).commit()
            sequence.release(0, point + delta, target).commit()
            QTest.qWait(30)

        for control in (settings.theme_mode, settings.autoplay, settings.hold,
                        settings.hold.lineEdit(), settings.live_key):
            scroll.ensureWidgetVisible(control)
            QCoreApplication.processEvents()
            before = bar.value()
            unchanged = (settings.theme_mode.currentIndex(), settings.autoplay.isChecked(),
                         settings.hold.value(), settings.live_key.keySequence().toString())
            touch_control(control, QPoint(3, -90))
            assert bar.value() == before + 90, 'Touch drag over settings field did not scroll'
            assert QApplication.activePopupWidget() is None, 'Scroll gesture opened a settings popup'
            assert unchanged == (settings.theme_mode.currentIndex(), settings.autoplay.isChecked(),
                                  settings.hold.value(), settings.live_key.keySequence().toString())
            routes.append(type(control).__name__)
        touch_control(settings.autoplay)
        _wait(lambda: settings.autoplay.isChecked())
        touch_control(settings.theme_mode)
        _wait(lambda: settings.theme_mode.view().isVisible())
        settings.theme_mode.hidePopup()
        touch_control(settings.hold.lineEdit())
        assert settings.hold.lineEdit().hasFocus(), 'Touch tap did not focus the spin editor'
        settings.hold.lineEdit().selectAll()
        QTest.keyClicks(settings.hold.lineEdit(), '650')
        QTest.keyClick(settings.hold.lineEdit(), Qt.Key.Key_Tab)
        assert settings.hold.value() == 650
        touch_control(settings.live_key)
        assert settings.live_key.hasFocus(), 'Touch tap did not focus the shortcut editor'
        QTest.keyClick(settings.live_key, Qt.Key.Key_J)
        assert settings.live_key.keySequence() == QKeySequence('J')
        settings.grab().save(str(report_dir / 'settings-touch-scroll.png'))
        settings.reject()

        viewer.resize(800, 600)
        viewer.show()
        viewer.activateWindow()
        viewer.setFocus()
        _wait(lambda: viewer.windowHandle() is not None and viewer.windowHandle().isExposed())
        image = QImage(2400, 1800, QImage.Format.Format_RGB32)
        image.fill('#5794b6')
        viewer.set_photo(image, Path('/synthetic/touch-double-tap.mov'))
        viewer.set_options(hold_ms=100)
        device, target = QTest.createTouchDevice(), viewer.windowHandle()
        sequence = QTest.touchEvent(target, device, False)
        with patch.object(viewer, '_start_playback') as playback:
            for factor in (2.0, 1.0):
                for index, origin in enumerate((QPoint(400, 300), QPoint(432, 322))):
                    point = target.mapFromGlobal(viewer.viewport().mapToGlobal(origin))
                    sequence.press(0, point, target).commit()
                    sequence.move(0, point + QPoint(6, 6), target).commit()
                    if index == 1:
                        QTest.qWait(180)
                    sequence.release(0, point + QPoint(6, 6), target).commit()
                assert viewer.is_zoom_animating(), 'Touch double-tap did not begin smooth zoom'
                _wait(lambda: not viewer.is_zoom_animating())
                assert abs(viewer.zoom_factor() - factor) < .001
            assert playback.call_count == 0, 'Double-tap briefly started Live playback'
        return {'touch_target': 'QWindow', 'settings_scroll_routes': routes,
                'drag_does_not_change_fields_or_open_popup': True,
                'tap_preserves_switch_combo_and_text_edit': True,
                'touch_double_tap_toggles_fit_and_twice_fit': True,
                'touch_jitter_and_second_contact_hold_do_not_play_live': True,
                'physical_touch_hardware': False}
    finally:
        settings.reject()
        settings.deleteLater()
        viewer.close()
        viewer.deleteLater()


def _check_thumbnail_live_badges(report_dir: Path):
    """Render both thumbnail layouts without decoding or replacing their model."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QWidget
    from .catalog_model import ThumbnailDelegate, _fitted_image_rect, _live_badge_layout
    from .theme import theme_tokens

    widget = QWidget()
    model = CatalogModel(widget)
    landscape = Path('/synthetic/Live-landscape.jpg')
    portrait = Path('/synthetic/Live-portrait.jpg')
    model.set_items([MediaItem('live-landscape', landscape, landscape.with_suffix('.MOV')),
                     MediaItem('live-portrait', portrait, portrait.with_suffix('.MOV')),
                     MediaItem('static-landscape', landscape),
                     MediaItem('static-portrait', portrait)])
    for path, width, height in ((landscape, 320, 180), (portrait, 100, 300)):
        photo = QImage(width, height, QImage.Format.Format_RGB32)
        photo.fill('#6cacc6')
        painter = QPainter(photo)
        painter.fillRect(0, height * 2 // 3, width, height // 3 + 1, QColor('#4c7753'))
        painter.fillRect(width * 2 // 3, height // 3, width // 6, height // 3, QColor('#e9bd70'))
        painter.end()
        model.put_thumbnail(path, photo)
    keys = {path: pixmap.cacheKey() for path, pixmap in model.thumbnails.items()}
    requests, resets = QSignalSpy(model.thumbnail_requested), QSignalSpy(model.modelReset)
    cases = ((True, 120, 0, 'dot', 20, 45),
             (True, 260, 0, 'full', 170, 45),
             (True, 260, 1, 'full', 450, 45),
             (False, 64, 0, 'dot', 20, 300),
             (False, 180, 0, 'full', 170, 300),
             (False, 180, 1, 'full', 450, 300))
    previews = []
    rendered = 0
    try:
        for theme in ('dark', 'light'):
            widget.setProperty('liveviewerTheme', theme)
            colors = theme_tokens(theme)
            for dpr in (1, 2):
                sheet = QImage(750 * dpr, 520 * dpr, QImage.Format.Format_ARGB32_Premultiplied)
                sheet.setDevicePixelRatio(dpr)
                sheet.fill(colors['surface'])
                sheet_painter = QPainter(sheet)
                for large, extent, row, expected, x, y in cases:
                    delegate = ThumbnailDelegate(widget, large=large)
                    (delegate.set_thumbnail_width if large else delegate.set_thumbnail_height)(extent)
                    option = QStyleOptionViewItem()
                    option.initFrom(widget)
                    option.widget = widget
                    size = delegate.sizeHint(option, model.index(row))
                    option.rect = QRect(0, 0, size.width(), size.height())
                    option.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Selected
                    images = []
                    for source_row in (row, row + 2):
                        image = QImage(size.width() * dpr, size.height() * dpr,
                                       QImage.Format.Format_ARGB32_Premultiplied)
                        image.setDevicePixelRatio(dpr)
                        image.fill(colors['surface'])
                        painter = QPainter(image)
                        delegate.paint(painter, option, model.index(source_row))
                        painter.end()
                        images.append(image)
                    image_area = option.rect.adjusted(10, 10, -10, -34)
                    photo_rect = _fitted_image_rect(image_area,
                                                   model.thumbnails[str(model.items[row].image_path)])
                    mode, badge = _live_badge_layout(image_area)
                    assert mode == expected and image_area.contains(badge), 'LIVE badge does not fit the thumbnail frame'
                    if row == 1:
                        assert not badge.intersects(photo_rect), 'Portrait LIVE badge should use the frame margin'
                        assert images[0].copy(photo_rect.x() * dpr, photo_rect.y() * dpr,
                                              photo_rect.width() * dpr, photo_rect.height() * dpr) == images[1].copy(
                                                  photo_rect.x() * dpr, photo_rect.y() * dpr,
                                                  photo_rect.width() * dpr, photo_rect.height() * dpr)
                    differences = 0
                    for py in range(images[0].height()):
                        for px in range(images[0].width()):
                            if images[0].pixel(px, py) != images[1].pixel(px, py):
                                differences += 1
                                assert badge.contains(px // dpr, py // dpr), 'LIVE mark spills outside its bounds'
                    assert differences > (500 if mode == 'full' else 15) * dpr * dpr
                    if mode == 'dot':
                        assert differences <= 100 * dpr * dpr
                        center = badge.center()
                        assert images[0].pixelColor(center.x() * dpr, center.y() * dpr) == QColor(colors['badge_text'])
                    sheet_painter.drawImage(x, y, images[0])
                    sheet_painter.setPen(QColor(colors['text']))
                    sheet_painter.drawText(x, y - 9, ('Overview' if large else 'Filmstrip') + f' {extent}: {mode}')
                    rendered += 1
                sheet_painter.end()
                name = f'thumbnail-live-badges-{theme}-dpr{dpr}.png'
                assert sheet.save(str(report_dir / name))
                previews.append(name)
        # Resizing the same delegate returns to the complete label immediately.
        delegate = ThumbnailDelegate(widget, large=True)
        modes = []
        for width in (120, 260, 120, 260):
            delegate.set_thumbnail_width(width)
            size = delegate.sizeHint(None, model.index(0))
            image_area = QRect(0, 0, size.width(), size.height()).adjusted(10, 10, -10, -34)
            modes.append(_live_badge_layout(image_area)[0])
        assert modes == ['dot', 'full', 'dot', 'full']
        assert {path: pixmap.cacheKey() for path, pixmap in model.thumbnails.items()} == keys
        assert requests.count() == 0 and resets.count() == 0
        return {'overview_and_filmstrip_share_adaptive_live_badge': True,
                'small_thumbnails_use_compact_dot': True,
                'large_thumbnails_restore_full_live_label': True,
                'thumbnail_frame_bounds_control_compact_mode': True,
                'portrait_badge_uses_frame_margin_without_covering_photo': True,
                'static_thumbnails_have_no_live_mark': True,
                'dark_light_selected_dpr1_dpr2_painted': True,
                'thumbnail_cache_identity_preserved': True,
                'thumbnail_requests_during_badge_resize': requests.count(),
                'model_resets_during_badge_resize': resets.count(),
                'painted_cases': rendered, 'previews': previews}
    finally:
        widget.deleteLater()


def _check_folder_append_remove(report_dir: Path):
    from PIL import Image
    from unittest.mock import patch
    from .app import MainWindow
    from .settings import Preferences
    previous = os.environ.get('LIVEGALLERY_DATA_DIR')
    window = None
    with tempfile.TemporaryDirectory(prefix='folder-management-', dir=report_dir) as directory:
        base = Path(directory).resolve()
        root, other = base / 'A 照片', base / 'B photos'
        branch, island = root / '已移除', root / '已移除' / '单独恢复'
        deeper = island / '再次移除'
        files = (root / 'keep.jpg', island / 'restore.png', deeper / 'deep.jpg',
                 branch / '兄弟目录' / 'stay-out.png', other / 'other.jpg')
        for number, path in enumerate(files):
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (900, 600), (30 + number * 35, 90, 130)).save(path)
        original = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in files}
        os.environ['LIVEGALLERY_DATA_DIR'] = str(base / 'settings')
        try:
            window = MainWindow(); window.resize(1280, 850); window.show()
            window.preferences.cache_location = 'library'
            with patch('livegallery.app.select_photo_folders', return_value=(root,)):
                window.open_button.click()
            _wait(lambda: not window._scan_pending and window._shown_image_path == files[0], 25)
            window.tree.set_checked_directories((root,))
            window.viewer._zoom_at(2, window.viewer._viewport_center())
            current, scale, center = window._current_id, window.viewer.transform(), window.viewer._scene_center()
            with patch('livegallery.app.select_photo_folders', return_value=(other, other / '.')):
                window.open_button.click()
            _wait(lambda: not window._scan_pending and len(window._items) == 5, 25)
            assert window.folders == (root, other)
            assert window.tree.checked_directories == {root, other}
            assert window._current_id == current and window.viewer.transform() == scale
            assert (window.viewer._scene_center() - center).manhattanLength() < 4
            generation = window._scan_generation
            with patch('livegallery.app.select_photo_folders', return_value=()):
                window.open_button.click()
            assert window._scan_generation == generation

            menu_labels = []
            def remove_menu():
                menu = QApplication.activePopupWidget()
                if menu is None:
                    QTimer.singleShot(10, remove_menu)
                    return
                menu_labels.append(menu.actions()[0].text())
                menu.actions()[0].trigger()
                menu.close()
            target = window.tree._nodes[branch]
            expected = window.tree.removal_count((branch,))
            QTimer.singleShot(0, remove_menu)
            window._folder_context_menu(window.tree.visualItemRect(target).center())
            _wait(lambda: not window._scan_pending and len(window._items) == 2, 25)
            assert menu_labels == [f'从 LiveViewer 移除 {expected} 个文件夹（含子文件夹）']
            window.search_scope.setCurrentIndex(window.search_scope.findData('all'))
            window.search.setText('restore')
            assert not window.model.items
            window.search.clear()
            with patch('livegallery.app.select_photo_folders', return_value=(island,)):
                window.open_button.click()
            _wait(lambda: not window._scan_pending and len(window._items) == 4, 25)
            assert branch not in window.tree.known_directories
            window._remove_folders((deeper,))
            _wait(lambda: not window._scan_pending and len(window._items) == 3, 25)
            window._rescan()
            _wait(lambda: not window._scan_pending, 25)
            assert {item.image_path for item in window._items} == {files[0], files[1], files[4]}
            _wait(lambda: window._shown_image_path == files[0]
                  and not window.viewer._photo.pixmap().isNull(), 25)
            window.grab().save(str(report_dir / 'folder-append-remove-restored-island.png'))
            window.close(); assert window.wait_for_shutdown(25); window.deleteLater()
            QCoreApplication.processEvents()
            window = MainWindow(); window.show()
            window.load_folders(window.preferences.last_folders)
            _wait(lambda: not window._scan_pending and len(window._items) == 3, 25)
            assert set(map(Path, window.preferences.excluded_folders)) == {branch, deeper}
            window._remove_folders(window.folders)
            _wait(lambda: not window._scan_pending and not window.folders, 25)
            assert not window.model.items and window._displayed_item is None
            assert Preferences.load().last_folders == [] and Preferences.load().last_folder == ''
            assert (root / 'LiveViewer Cache').is_dir() and (other / 'LiveViewer Cache').is_dir()
            window.add_folders((root,))
            _wait(lambda: not window._scan_pending and len(window._items) == 4, 25)
            assert not any(Path(path).is_relative_to(root) for path in window.preferences.excluded_folders)
            assert {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in files} == original
            return {'append_preserves_checks_and_zoom': True, 'context_action_count': expected,
                    'persistent_subtree_exclusion_and_island_reimport': True,
                    'remove_all_and_reimport': True, 'original_files_unchanged': True}
        finally:
            if window is not None:
                window.close(); assert window.wait_for_shutdown(25)
                window.deleteLater(); QCoreApplication.processEvents()
            if previous is None:
                os.environ.pop('LIVEGALLERY_DATA_DIR', None)
            else:
                os.environ['LIVEGALLERY_DATA_DIR'] = previous


def _check_folder_tree_selection(report_dir: Path):
    """Real sidebar targets, stable background refresh and one-commit drag boxes."""
    from PIL import Image
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QSignalSpy
    from .app import MainWindow
    from .ui_theme import application_theme, tokens_for
    previous = os.environ.get('LIVEGALLERY_DATA_DIR')
    controller = application_theme()
    original_mode = controller.mode
    window = None
    with tempfile.TemporaryDirectory(prefix='sidebar-selection-', dir=report_dir) as directory:
        base = Path(directory).resolve()
        root = base / '照片目录'
        folders = [root / f'F{number:02d}' for number in range(24)]
        for folder in folders:
            folder.mkdir(parents=True)
            Image.new('RGB', (80, 60), '#496b85').save(folder / 'one.jpg')
        os.environ['LIVEGALLERY_DATA_DIR'] = str(base / 'settings')
        try:
            window = MainWindow(); window.resize(1280, 900); window.show()
            window.load_folder(root)
            _wait(lambda: not window._scan_pending and len(window._items) == 24, 25)
            tree = window.tree
            counts = {folder: 1 for folder in folders}
            tree.set_checked_directories(())
            target = folders[15]
            tree.scrollToItem(tree._nodes[target], tree.ScrollHint.PositionAtCenter)
            QCoreApplication.processEvents()
            box = tree.checkbox_rect(tree._nodes[target])
            assert box.width() == box.height() == 24
            assert tree.visualItemRect(tree._nodes[target]).height() >= 38
            point = box.bottomRight() - QPoint(1, 1)
            scroll = tree.verticalScrollBar().value()
            for _ in range(3):
                tree.set_directories((root,), counts, tree.checked_directories)
                QCoreApplication.processEvents()
            assert tree.verticalScrollBar().value() == scroll
            assert tree.itemAt(point) is tree._nodes[target]
            checked, resets = QSignalSpy(tree.checkedChanged), QSignalSpy(window.model.modelReset)
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
            _wait(lambda: window._shown_image_path == target / 'one.jpg', 12)
            assert tree.checked_directories == {target}
            assert {item.image_path.parent for item in window.model.items} == {target}
            assert checked.count() == resets.count() == 1
            handle = window.windowHandle()
            assert handle is not None and handle.isExposed()
            device = QTest.createTouchDevice()
            touch = QTest.touchEvent(handle, device, False)
            local = tree.viewport().mapTo(window, point)
            touch.press(0, local, handle).commit(); touch.release(0, local, handle).commit()
            QCoreApplication.processEvents()
            assert not tree.checked_directories and not window.model.items

            def band(modifiers=Qt.KeyboardModifier.NoModifier, *, stop=18):
                first, last = tree._nodes[folders[15]], tree._nodes[folders[stop]]
                start = QPoint(tree.viewport().width() - 4, tree.visualItemRect(first).top() + 2)
                end = QPoint(start.x() - 55, tree.visualItemRect(last).bottom() - 2)
                changes, models = QSignalSpy(tree.checkedChanged), QSignalSpy(window.model.modelReset)
                QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, modifiers, start)
                for fraction in (.2, .5, .8, 1):
                    QTest.mouseMove(tree.viewport(), start + (end - start) * fraction)
                    QCoreApplication.processEvents()
                    assert changes.count() == models.count() == 0
                assert tree._rubber_band.isVisible()
                if modifiers == Qt.KeyboardModifier.NoModifier:
                    window.grab().save(str(report_dir / 'folder-tree-rubber-band-selection.png'))
                QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, modifiers, end)
                QCoreApplication.processEvents()
                assert changes.count() == models.count() == 1
                assert not tree._rubber_band.isVisible()
            band()
            assert tree.checked_directories == set(folders[15:19])
            band(Qt.KeyboardModifier.ControlModifier, stop=16)
            assert tree.checked_directories == set(folders[17:19])
            band(Qt.KeyboardModifier.ShiftModifier, stop=16)
            assert tree.checked_directories == set(folders[15:19])

            point = tree.checkbox_rect(tree._nodes[target]).center()
            baseline = tree.checked_directories
            QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
            extra = root / 'added-empty'; extra.mkdir()
            tree.set_directories((root,), {**counts, extra: 0}, baseline)
            QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert tree.checked_directories == baseline
            for mode in ('dark', 'light'):
                controller.set_mode(mode)
                QCoreApplication.processEvents()
                tree.setFocus()
                assert tree.palette().color(tree.palette().ColorRole.Highlight).name() == tokens_for(tree)['selected']
                for ratio in (1, 2):
                    viewport = tree.viewport()
                    image = QImage(viewport.width() * ratio, viewport.height() * ratio, QImage.Format.Format_ARGB32)
                    image.setDevicePixelRatio(ratio)
                    image.fill(QColor(tokens_for(tree)['surface']))
                    viewport.render(image)
                    assert image.save(str(report_dir / f'folder-tree-{mode}-dpr{ratio}.png'))
                assert tree.grab().save(str(report_dir / f'folder-tree-raw-{mode}.png'))
                assert window.grab().save(str(report_dir / f'folder-tree-raw-window-{mode}.png'))
            if hasattr(window, 'path_group'):
                assert window.sidebar_button.parentWidget() is window.path_group
                window.sidebar_button.click(); assert not window.sidebar.isVisible()
                window.sidebar_button.click(); assert window.sidebar.isVisible()
                assert window.sidebar_button.text() == '隐藏列表'
            return {'checkbox_logical_size': [24, 24], 'minimum_row_height': 38,
                    'same_structure_refresh_keeps_target_and_scroll': True,
                    'paint_and_edge_hit_rect_match': True, 'row_click_filters_actual_gallery': True,
                    'rubber_band_no_update_before_release': True, 'rubber_band_single_commit': True,
                    'control_toggle_shift_add': True, 'structural_refresh_cancels_late_release': True,
                    'checkbox_touch_target': 'QWindow', 'physical_touch_hardware': False,
                    'native_selection_underlay_matches_theme': True,
                    'theme_dpr_previews': ['dark-1', 'dark-2', 'light-1', 'light-2']}
        finally:
            if window is not None:
                window.close(); assert window.wait_for_shutdown(25)
                window.deleteLater(); QCoreApplication.processEvents()
            controller.set_mode(original_mode)
            if previous is None:
                os.environ.pop('LIVEGALLERY_DATA_DIR', None)
            else:
                os.environ['LIVEGALLERY_DATA_DIR'] = previous


def _check_multiple_folder_collection(fixtures: Path, report_dir: Path):
    from PIL import Image
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QTreeWidgetItemIterator
    from .app import MainWindow
    from .settings import Preferences

    previous_data = os.environ.get('LIVEGALLERY_DATA_DIR')
    window = None
    with tempfile.TemporaryDirectory(prefix='multi-import-', dir=report_dir) as directory:
        base = Path(directory).resolve()
        first, second = base / 'A 照片', base / 'B photos'
        child = first / '子目录'
        child.mkdir(parents=True)
        second.mkdir()
        for folder in (first, second):
            shutil.copy2(fixtures / 'VFR_PAIR.jpg', folder / 'shared.jpg')
            shutil.copy2(fixtures / 'VFR_PAIR.MOV', folder / 'shared.MOV')
        Image.new('RGB', (180, 140), '#247596').save(child / 'child.png')
        Image.new('RGB', (180, 140), '#934a62').save(second / 'other.png')
        roots = (first, second, child)
        os.environ['LIVEGALLERY_DATA_DIR'] = str(base / 'settings')
        try:
            window = MainWindow()
            window.resize(1280, 850)
            window.show()
            window.preferences.cache_location = 'library'
            window.load_folders((*roots, first))
            _wait(lambda: not window._scan_pending and len(window._primary_items) == 4, 25)
            assert window.folders == roots and len(window.model.items) == 4
            assert len({str(item.image_path) for item in window._primary_items}) == 4
            assert len(window.cache.actual_paths) == 3
            for root in roots:
                assert (root / 'LiveViewer Cache').is_dir()
            source = next(item for item in window._primary_items if item.image_path.parent == first)
            sibling = next(item for item in window._primary_items if item.image_path == second / 'shared.jpg')
            window.cache.set_override(source, source.image_path)
            assert window.cache.get_override(sibling) is None
            for item in window._primary_items:
                owner = child if item.image_path.is_relative_to(child) else first if item.image_path.is_relative_to(first) else second
                thumbnail = window.cache.get_thumbnail(item.image_path)
                assert thumbnail.is_relative_to(owner / 'LiveViewer Cache')

            def node(path):
                iterator = QTreeWidgetItemIterator(window.tree)
                while iterator.value() is not None:
                    item = iterator.value()
                    if item.data(0, Qt.ItemDataRole.UserRole) == str(path):
                        return item
                    iterator += 1
                raise AssertionError('Imported folder is missing from the sidebar')

            def click(path, *, checkbox=False, modifiers=Qt.KeyboardModifier.NoModifier):
                item = node(path)
                rect = window.tree.visualItemRect(item)
                if checkbox:
                    option = QStyleOptionViewItem()
                    window.tree.itemDelegate().initStyleOption(option, window.tree.indexFromItem(item))
                    option.rect = rect
                    point = window.tree.style().subElementRect(
                        QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, window.tree.viewport()).center()
                else:
                    point = QPoint(rect.left() + 100, rect.center().y())
                QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, point)
                QCoreApplication.processEvents()

            click(first)
            assert len(window.model.items) == 1 and window.model.items[0].image_path.parent == first
            click(second, modifiers=Qt.KeyboardModifier.ShiftModifier)
            assert window.tree.checked_directories == set(roots) and len(window.model.items) == 4
            click(child, checkbox=True)
            assert window.tree.checked_directories == {first, second} and len(window.model.items) == 3
            window.tree.setFocus()
            QTest.keyClick(window.tree, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            assert window.tree.checked_directories == set(roots) and len(window.model.items) == 4
            click(first)
            assert [window.search_scope.itemText(i) for i in range(window.search_scope.count())] == ['已选文件夹', '全局']
            window.search_scope.setCurrentIndex(window.search_scope.findData('current'))
            window.search.setText('shared')
            assert len(window.model.items) == 1
            window.search_scope.setCurrentIndex(window.search_scope.findData('all'))
            assert len(window.model.items) == 2
            window.search.clear()
            assert len(window.model.items) == 1
            window._toggle_grid(True)
            window.grab().save(str(report_dir / 'multiple-folders-selected-overview.png'))

            for location in ('user', 'program', 'library'):
                window.preferences.cache_location = location
                window.load_folders(roots, preserve_view=True)
                _wait(lambda: not window._scan_pending and len(window._primary_items) == 4
                      and window._cache_key[0] == location, 25)
                assert len(window.model.items) == 1 and window.tree.checked_directories == {first}
                assert window.cache.get_override(source) == source.image_path
                assert window.cache.get_override(sibling) is None
                if location == 'library':
                    assert not window.cache.has_fallbacks, 'The app mistook its previous cache writer for another process'
            saved = Preferences.load()
            assert saved.last_folders == [str(root) for root in roots]
            assert saved.selected_folders == [str(first)] and saved.search_scope == 'all'
            window.close()
            assert window.wait_for_shutdown(15)
            window.deleteLater()
            QCoreApplication.processEvents()
            window = MainWindow()
            window.show()
            window.load_folders(window.preferences.last_folders)
            _wait(lambda: not window._scan_pending and len(window._primary_items) == 4, 25)
            assert window.tree.checked_directories == {first} and len(window.model.items) == 1
            assert window.search_scope.currentData() == 'all'
            return {'imported_roots': 3, 'items': 4, 'overlapping_roots_and_items_deduplicated': True,
                    'checkboxes_control_exact_directories': True, 'shift_range_and_ctrl_a': True,
                    'search_labels': ['已选文件夹', '全局'], 'selected_and_global_search': True,
                    'clearing_search_restores_selected_folders': True,
                    'three_cache_modes_and_writer_handoff': True, 'root_choices_isolated': True,
                    'folders_checks_and_search_scope_survive_restart': True}
        finally:
            if window is not None:
                window.close()
                assert window.wait_for_shutdown(15)
                window.deleteLater()
                QCoreApplication.processEvents()
            if previous_data is None:
                os.environ.pop('LIVEGALLERY_DATA_DIR', None)
            else:
                os.environ['LIVEGALLERY_DATA_DIR'] = previous_data


def _check_library_cache(fixtures: Path, report_dir: Path):
    """Exercise real cache files without writing next to shipped test media."""
    from PIL import Image
    from . import cache as cache_module
    from .cache_locations import CACHE_DIRECTORY, is_managed_cache_directory, open_cache
    from .models import PairDecision

    with tempfile.TemporaryDirectory(prefix='library-cache-', dir=report_dir) as temporary:
        base = Path(temporary)
        gallery = base / '照片图库'
        nested = gallery / '2026'
        nested.mkdir(parents=True)
        shutil.copy2(fixtures / 'Live_H264.jpg', nested / 'Live.jpg')
        shutil.copy2(fixtures / 'Live_HEVC.jpeg', nested / 'Live.jpeg')
        shutil.copy2(fixtures / 'Live_H264.MOV', nested / 'Live.MOV')
        Image.new('RGB', (64, 48), '#365579').save(gallery / 'ordinary.bmp')
        user, program = base / 'user-data', base / 'program-data'
        arguments = {'user_base': user, 'program_base': program, 'max_bytes': 8 * 1024 ** 2}
        bindings = []
        def opened(location, root=gallery, **kwargs):
            binding = open_cache(location, root, **arguments, **kwargs)
            bindings.append(binding)
            return binding
        def close_all():
            for binding in reversed(bindings):
                binding.close()
            bindings.clear()
        try:
            local = opened('user')
            portable = opened('program')
            assert local.actual_path == user / 'cache'
            assert portable.actual_path == program / 'cache'
            assert not (gallery / CACHE_DIRECTORY).exists(), 'Default cache modes wrote into the photo folder'
            original_items = scan_folder(gallery)
            assert len(original_items) == 2
            live = next(item for item in original_items if item.is_live)
            still = nested / 'Live.jpg'
            other = nested / 'Live.jpeg'
            old_thumbnail = local.cache.get_thumbnail(still)
            thumbnail_digest = _file_digest(old_thumbnail)
            local.cache.set_override(live, still)
            decision = PairDecision(still, (other,), False, 'Synthetic v0.7 cache check')
            local.cache.put_decision(live, 'selftest-library-v1', decision)
            selected = opened('library', legacy_sources=(local.cache,))
            assert selected.location == 'library' and not selected.warning
            assert selected.actual_path == gallery / CACHE_DIRECTORY
            assert is_managed_cache_directory(selected.actual_path)
            hidden = None
            if sys.platform == 'win32':
                import ctypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
                kernel.GetFileAttributesW.restype = ctypes.c_uint32
                attributes = kernel.GetFileAttributesW(str(selected.actual_path))
                assert attributes != 0xffffffff and attributes & 0x2, 'Library cache is not hidden on Windows'
                hidden = True
            original_decoder = cache_module.thumbnail_bytes
            def unexpected_decode(*_args, **_kwargs):
                raise AssertionError('A valid existing thumbnail was decoded again')
            cache_module.thumbnail_bytes = unexpected_decode
            try:
                migrated = selected.cache.get_thumbnail(still)
                assert _file_digest(migrated) == thumbnail_digest
                assert selected.cache.get_decision(live, 'selftest-library-v1') == decision
                assert selected.cache.get_override(live) == still
            finally:
                cache_module.thumbnail_bytes = original_decoder
            thumbnail_name = migrated.name
            child = opened('library', nested)
            child.cache.get_thumbnail(still)
            assert len(scan_folder(gallery)) == 2, 'Cache thumbnails appeared as photos'
            assert scan_folder(selected.actual_path) == []
            child.close()
            selected.close()
            assert not local.cache._closed, 'Closing a migrated destination closed the caller-owned source'
            close_all()

            moved = base / '移动后的图库'
            gallery.rename(moved)
            moved_live = next(item for item in scan_folder(moved) if item.is_live)
            moved_still, moved_other = moved / '2026/Live.jpg', moved / '2026/Live.jpeg'
            moved_binding = opened('library', moved)
            cache_module.thumbnail_bytes = unexpected_decode
            try:
                reused = moved_binding.cache.get_thumbnail(moved_still)
                assert reused.name == thumbnail_name and _file_digest(reused) == thumbnail_digest
                assert moved_binding.cache.get_override(moved_live) == moved_still
                rebased = moved_binding.cache.get_decision(moved_live, 'selftest-library-v1')
                assert rebased is not None and rebased.image_path == moved_still
                assert rebased.extra_images == (moved_other,)
            finally:
                cache_module.thumbnail_bytes = original_decoder
            close_all()

            copied = base / '复制后的图库'
            shutil.copytree(moved, copied)
            first = opened('library', moved)
            second = opened('library', copied)
            copied_live = next(item for item in scan_folder(copied) if item.is_live)
            second.cache.set_override(copied_live, copied / '2026/Live.jpeg')
            assert first.cache.get_override(moved_live) == moved_still, 'Independent gallery copies shared mutable state'
            busy = opened('library', moved)
            assert busy.fallback and busy.warning
            assert busy.actual_path != moved / CACHE_DIRECTORY
            busy.cache.set_override(moved_live, moved_other)
            busy.close()
            close_all()
            restored = opened('library', moved)
            assert restored.cache.get_override(moved_live) == moved_other, 'Fallback manual choice was lost'
            restored.cache.clear_thumbnails()
            assert restored.cache.get_override(moved_live) == moved_other

            plain = moved / 'ordinary.bmp'
            before_thumbnail = restored.cache.get_thumbnail(plain)
            before_bytes = _file_digest(before_thumbnail)
            before_status = plain.stat()
            Image.new('RGB', (64, 48), '#b34d3e').save(plain)
            assert plain.stat().st_size == before_status.st_size
            os.utime(plain, ns=(before_status.st_atime_ns, before_status.st_mtime_ns))
            verified = restored.cache.verify_files([plain])
            assert verified['checked'] == 1 and plain in verified['changed'] and not verified['cancelled']
            assert _file_digest(restored.cache.get_thumbnail(plain)) != before_bytes
            assert restored.cache.get_override(moved_live) == moved_other
            cancelled = restored.cache.verify_files([plain], cancel=lambda: True)
            assert cancelled['cancelled'] and cancelled['checked'] == 0
            return {'three_locations': True, 'windows_hidden': hidden,
                    'managed_cache_excluded_from_scan': True,
                    'legacy_thumbnail_reused_without_decode': True,
                    'moved_gallery_reuses_thumbnail_decision_and_choice': True,
                    'copied_galleries_independent': True,
                    'writer_lock_falls_back_locally': True,
                    'fallback_and_clear_preserve_manual_choice': True,
                    'same_metadata_content_change_detected': True, 'deep_verification_cancelled': True}
        finally:
            close_all()


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
            'application_version': __version__,
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
        thumbnail_badges = _check_thumbnail_live_badges(report_dir)
        report['checks'].append({'check': 'thumbnail_live_badges_adapt_to_frame_size',
                                 **thumbnail_badges, 'status': 'passed'})
        pointer_lifecycle = _check_pointer_contact_lifecycle(fixtures)
        report['checks'].append({'check': 'pointer_contact_leave_double_tap_and_mixed_input',
                                 **pointer_lifecycle, 'status': 'passed'})
        touch_controls = _check_touch_scroll_and_double_tap(report_dir)
        report['checks'].append({'check': 'settings_scroll_and_double_tap_native_touch',
                                 **touch_controls, 'status': 'passed'})
        library_cache = _check_library_cache(fixtures, report_dir)
        report['checks'].append({'check': 'cache_locations_portable_library_reuse_and_validation',
                                 **library_cache, 'status': 'passed'})
        collection = _check_multiple_folder_collection(fixtures, report_dir)
        report['checks'].append({'check': 'multiple_folders_checkboxes_and_search_scope',
                                 **collection, 'status': 'passed'})
        folder_management = _check_folder_append_remove(report_dir)
        report['checks'].append({'check': 'folder_import_append_remove_and_restore',
                                 **folder_management, 'status': 'passed'})
        tree_selection = _check_folder_tree_selection(report_dir)
        report['checks'].append({'check': 'folder_tree_large_targets_rubber_band_and_refresh',
                                 **tree_selection, 'status': 'passed'})
        exact_frames = _check_exact_frame_reader(fixtures, report_dir)
        report['checks'].append({'check': 'exact_frame_index_export_h264_hevc_rotated_and_vfr',
                                 'videos': exact_frames, 'status': 'passed'})
        heic_roundtrip = _check_heic_cover_roundtrip(fixtures, report_dir)
        report['checks'].append({'check': 'genuine_heic_cover_copy_overwrite_full_decode',
                                 **heic_roundtrip, 'status': 'passed'})

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
            QTest.mouseRelease(viewer.viewport(), Qt.MouseButton.LeftButton, pos=center)
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
        navigator = _check_zoom_navigation_overview(viewer, fixtures, report_dir)
        report['checks'].append({'check': 'zoom_navigation_thumbnail_and_viewport_rectangle',
                                 **navigator, 'status': 'passed'})
        pointer_capability = _check_pointer_input_capability(viewer)
        report['checks'].append({'check': 'native_pointer_observer_capabilities',
                                 **pointer_capability, 'status': 'passed'})
        double_drag = _check_zoomed_double_tap_drag(
            viewer, fixtures, diagnostics_path=report_dir / 'zoomed-double-tap-drag-diagnostics.json')
        report['checks'].append({'check': 'zoomed_double_tap_drag_does_not_play_live',
                                 **double_drag, 'status': 'passed'})
        touch_menu = _check_blank_touch_context_menu(viewer, report_dir)
        report['checks'].append({'check': 'blank_touch_context_menu_survives_release',
                                 **touch_menu, 'status': 'passed'})
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
        copied_path = window._displayed_item.image_path
        before = copied_path.stat()
        assert window.filename_label.isReadOnly()
        assert window.filename_label.text() == copied_path.name
        window.activateWindow()
        window.filename_label.setFocus()
        QTest.keyClick(window.filename_label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert window.filename_label.selectedText() == copied_path.name
        QTest.keyClick(window.filename_label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QCoreApplication.processEvents()
        assert app.clipboard().text() == copied_path.name, 'Filename copy did not reach the clipboard'
        after = copied_path.stat()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        window.viewer.setFocus()
        report['checks'].append({'check': 'readonly_filename_selection_and_clipboard_copy',
                                 'file_unchanged': True, 'status': 'passed'})
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
        overview_zoom = _check_overview_touch_scroll(window, fixtures, report_dir)
        window.grab().save(str(report_dir / 'fullscreen-overview.png'))
        QTest.keyClick(window.grid, Qt.Key.Key_Escape)
        assert not window._fullscreen_active and window._grid_mode
        report['checks'].append({'check': 'fullscreen_preserves_overview_and_touch_scroll',
                                 'virtual_rows': 3000, 'swipe_never_opens_photo': True,
                                 'status': 'passed'})
        report['checks'].append({'check': 'overview_pinch_and_keyboard_thumbnail_zoom',
                                 **overview_zoom, 'status': 'passed'})
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
        filter_menu = window.kind_filter.menu
        filter_menu.popup(window.kind_filter.mapToGlobal(QPoint(0, window.kind_filter.height())))
        QCoreApplication.processEvents()
        for expected in (False, True):
            QTest.mouseClick(window.kind_filter.live, Qt.MouseButton.LeftButton,
                             pos=window.kind_filter.live.rect().center())
            QCoreApplication.processEvents()
            assert window.kind_filter.live.isChecked() == expected
            assert filter_menu.isVisible(), 'Selecting a filter closed the multi-select menu'
        filter_blank_result = _check_filter_blank_click(window)
        filter_menu.grab().save(str(report_dir / 'overview-filter.png'))
        filter_menu.hide()
        report['checks'].append({'check': 'overview_multi_select_type_filter',
                                 **filter_blank_result,
                                 'checkbox_click_keeps_menu_open': True, 'status': 'passed'})

        live_row = next(i for i, value in enumerate(window.model.items)
                        if value.is_live and value.image_path.stem == 'JPEG_PAIR')
        window.activateWindow()
        window.viewer.setFocus()
        window.preferences.autoplay = True
        window.viewer.set_options(loop=True)
        window._select_row(live_row)
        _wait(lambda: window.viewer._video.isVisible(), 18)
        _wait(lambda: not window.viewer._video.isVisible(), 8)
        assert window.viewer._photo.isVisible()
        window.preferences.autoplay = False
        report['checks'].append({'check': 'selection_autoplay_once_independent_of_hold_loop', 'status': 'passed'})
        window._select_row(live_row)
        _wait(lambda: window._displayed_item is not None
              and window._displayed_item.image_path.stem == 'JPEG_PAIR', 18)
        filmstrip_touch = _check_filmstrip_touch(window, report_dir)
        report['checks'].append({'check': 'filmstrip_single_finger_scroll_and_tap',
                                 **filmstrip_touch, 'status': 'passed'})
        filmstrip_resize = _check_filmstrip_resize(window, report_dir)
        report['checks'].append({'check': 'filmstrip_mouse_touch_resize_and_viewer_fit',
                                 **filmstrip_resize, 'status': 'passed'})
        filmstrip_layouts = _check_filmstrip_layouts(window, report_dir)
        report['checks'].append({'check': 'filmstrip_three_layouts_resize_and_persistence',
                                 **filmstrip_layouts, 'status': 'passed'})
        detached = _check_detached_preview_windows(window, fixtures, report_dir)
        report['checks'].append({'check': 'detached_preview_reference_map_and_window_lifecycle',
                                 **detached, 'status': 'passed'})
        reference_map = _check_reference_touch_map(window, fixtures, report_dir)
        report['checks'].append({'check': 'reference_map_native_touch_gestures_and_ownership',
                                 **reference_map, 'status': 'passed'})
        edge_gestures = _check_fullscreen_edge_gestures(window, report_dir)
        report['checks'].append({'check': 'fullscreen_edge_swipes_follow_settings_and_escape_state',
                                 **edge_gestures, 'status': 'passed'})
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
        badge = window.fullscreen_live_badge
        assert badge.width() >= badge.sizeHint().width() and badge.height() >= badge.sizeHint().height(), 'LIVE badge clips styled contents'
        assert badge.contentsRect().width() >= badge.fontMetrics().horizontalAdvance(badge.text()), 'LIVE badge clips its text'
        badge.grab().save(str(report_dir / 'fullscreen-live-badge.png'))
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
        assert window.collapse_strip_button.isVisible()
        QTest.mouseClick(window.collapse_strip_button, Qt.MouseButton.LeftButton)
        assert not window.film.isVisible() and not window.strip_button.isChecked()
        assert not window.preferences.filmstrip_visible and window._fullscreen_active
        QTest.keyClick(window.viewer, Qt.Key.Key_F)
        assert window.film.isVisible() and window.strip_button.isChecked()
        report['checks'].append({'check': 'filmstrip_top_arrow_hides_and_shortcut_restores',
                                 'fullscreen_stays_active': True, 'status': 'passed'})
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
                                 'live_only': True, 'two_escape_sequence': True, 'badge_fits_styled_text': True,
                                 'badge_size': [badge.width(), badge.height()], 'status': 'passed'})

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
            reveal = _check_windows_explorer_selection(report_dir)
            report['checks'].append({'check': 'windows_explorer_file_reveal',
                                     **reveal, 'status': 'passed'})

        settings = SettingsDialog(window.preferences, window)
        assert not settings.live_key.keySequence().isEmpty()
        assert not settings.live_once_key.keySequence().isEmpty()
        assert not settings.filmstrip_key.keySequence().isEmpty()
        assert not settings.autoplay.isChecked()
        assert window.preferences.horizontal_scroll_mode == 'navigate'
        assert settings.horizontal_scroll_mode.currentIndex() == 0
        assert [settings.cache_location.itemData(index) for index in range(settings.cache_location.count())] == [
            'user', 'program', 'library']
        assert settings.cache_location.currentData() == window.preferences.cache_location
        settings.horizontal_scroll_mode.setCurrentIndex(1)
        assert settings.horizontal_scroll_mode.currentIndex() == 1
        settings.live_once_key.setKeySequence(QKeySequence('F9'))
        assert settings.live_once_key.keySequence().toString() == 'F9'
        settings.show()
        QCoreApplication.processEvents()
        settings_result = _check_settings_controls(settings, window, report_dir)
        report['checks'].append({'check': 'settings_wheel_safety_and_blue_switches',
                                 **settings_result, 'status': 'passed'})
        settings.grab().save(str(report_dir / 'settings.png'))
        settings.findChild(QScrollArea).ensureWidgetVisible(settings.portable)
        QCoreApplication.processEvents()
        settings.grab().save(str(report_dir / 'settings-cache.png'))
        settings.close()
        report['checks'].append({'check': 'main_window_scan_navigation_grid_and_settings', 'status': 'passed'})
        theme_result = _check_application_theme(window, report_dir)
        report['checks'].append({'check': 'application_appearance_modes_and_system_changes',
                                 **theme_result, 'status': 'passed'})
        editor_result = _check_real_cover_editor(fixtures, report_dir, window)
        preview_export = editor_result.pop('preview_export')
        report['checks'].append({'check': 'real_vfr_cover_editor_copy_and_cancel',
                                 **editor_result, 'status': 'passed'})
        report['checks'].append({'check': 'cover_editor_preview_and_standalone_frame_export',
                                 **preview_export, 'status': 'passed'})
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


def _check_detached_preview_windows(window, fixtures: Path, report_dir: Path):
    """Use the actual app windows, shared media player and native touch splitters."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QLabel
    from .app import MainWindow
    from .detached_windows import accessible_geometry
    from .settings import Preferences

    fields = ('map_dock_edge', 'map_dock_width', 'map_bottom_height', 'map_floating',
              'preview_geometry', 'preview_screen', 'map_float_geometry', 'map_float_screen')
    original = {name: getattr(window.preferences, name) for name in fields}
    geometry = window.saveGeometry()
    original_id, grid_mode = window._current_id, window._grid_mode
    autoplay = window.preferences.autoplay
    sidebar_checked = window.sidebar_button.isChecked()
    viewer, player, model = window.viewer, window.viewer._player, window.model
    shutdown_probe = None
    report_dir.mkdir(parents=True, exist_ok=True)

    def settle():
        for _ in range(5):
            QCoreApplication.processEvents()
            QTest.qWait(1)

    def native_frame(target):
        assert not (target.windowFlags() & Qt.WindowType.FramelessWindowHint)
        if sys.platform == 'win32' and QApplication.instance().platformName() == 'windows':
            import ctypes
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.IsWindow.argtypes, user32.IsWindow.restype = [ctypes.c_void_p], ctypes.c_int
            user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            handle = int(target.winId())
            assert user32.IsWindow(handle), 'Auxiliary window has no real HWND'
            style = user32.GetWindowLongPtrW(handle, -16)
            assert style & 0x00c00000 == 0x00c00000, 'Native title bar is missing'
            assert style & 0x00040000, 'Native resize frame is missing'
            return True
        return False

    def drag(amount, touch=False):
        panel = window.touch_map_panel
        handle = panel.resize_handle
        edge = window.preferences.map_dock_edge
        delta = QPoint(0, -amount) if edge == 'bottom' else QPoint(amount if edge == 'left' else -amount, 0)
        start = handle.rect().center()
        end = handle.mapToGlobal(start) + delta
        if touch:
            target = window.windowHandle()
            assert target is not None and target.isExposed()
            sequence = QTest.touchEvent(target, QTest.createTouchDevice(), False)
            point = handle.mapTo(window, start)
            sequence.press(0, point, target).commit()
            sequence.move(0, window.mapFromGlobal(end), target).commit()
            settle()
            sequence.release(0, window.mapFromGlobal(end), target).commit()
        else:
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(handle, handle.mapFromGlobal(end))
            settle()
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(end))
        settle()

    try:
        window.preferences.autoplay = False
        window._toggle_grid(False)
        window.activateWindow()
        window.viewer.setFocus()
        settle()
        assert window.sidebar_button.parentWidget() is window.path_group
        assert not window.header.isAncestorOf(window.sidebar_button)
        window.path_label.setText('已打开 3 个文件夹')
        settle()
        assert window.sidebar_button.x() - (window.path_label.x() + window.path_label.width()) <= 8
        window.sidebar_button.setChecked(True)
        window._toggle_sidebar(True)
        QTest.mouseClick(window.sidebar_button, Qt.MouseButton.LeftButton)
        assert window.sidebar_button.text() == '显示列表' and not window.sidebar.isVisible()
        window._toggle_fullscreen()
        settle()
        assert not window.filterbar.isVisible()
        window._toggle_fullscreen()
        settle()
        assert not window.sidebar.isVisible() and window.sidebar_button.text() == '显示列表'
        QTest.mouseClick(window.sidebar_button, Qt.MouseButton.LeftButton)
        assert window.sidebar.isVisible() and window.sidebar_button.text() == '隐藏列表'
        window.path_label.setText('/a very long photo directory/' * 30)
        window.path_label.setToolTip(window.path_label.text())
        settle()
        assert '…' in QLabel.text(window.path_label) and window.sidebar_button.isVisible()
        window._update_path_label()
        viewer.fit_image()
        viewer._zoom_at(2, viewer._viewport_center())
        transform, center = viewer.transform(), viewer._scene_center()
        QTest.mouseClick(window.detach_button, Qt.MouseButton.LeftButton)
        settle()
        preview = window.preview_window
        assert window._detached and preview.isWindow() and preview.windowHandle().isExposed()
        assert preview.isMaximized() and not preview.isFullScreen() and not preview.isMinimized()
        preview_frame_verified = native_frame(preview)
        assert window.viewer is viewer and viewer._player is player and window.model is model
        assert preview.content.currentWidget() is viewer and window.content.currentWidget() is window.grid
        assert window._grid_mode and not window.film.isVisible()
        assert abs(viewer.transform().m11() - transform.m11()) < .00001
        assert (viewer._scene_center() - center).manhattanLength() < 5
        assert not viewer._play_requested and not window.map_button.isChecked()
        assert window.map_button.text() == '虚拟触控板'
        assert not preview.isAncestorOf(window.film) and not preview.isAncestorOf(window.header)
        preview.grab().save(str(report_dir / 'detached-preview.png'))
        window.grab().save(str(report_dir / 'detached-main-overview.png'))

        # Selection and actual autoplay belong to the photo in the preview,
        # even though the main window continues to display the overview.
        live_row = next(i for i, item in enumerate(model.items) if item.image_path.stem == 'JPEG_PAIR')
        window.preferences.autoplay = True
        window._grid_selected(model.index(live_row))
        _wait(lambda: viewer._video.isVisible(), 18)
        assert window._grid_mode and window.content.currentWidget() is window.grid
        _wait(lambda: not viewer._video.isVisible(), 8)
        window.preferences.autoplay = False
        original_live_id = window._current_id
        QTest.mouseClick(window.map_button, Qt.MouseButton.LeftButton)
        settle()
        panel = window.touch_map_panel
        assert panel.isVisible() and panel.edge == window.preferences.map_dock_edge
        assert panel.surface.viewer is viewer
        assert not preview.isAncestorOf(panel)
        for edge in ('right', 'left', 'bottom'):
            window._dock_touch_map(edge)
            settle()
            before = panel.height() if edge == 'bottom' else panel.width()
            available = window.content.height() if edge == 'bottom' else window.content.width()
            drag(30)
            assert (panel.height() if edge == 'bottom' else panel.width()) > before
            assert (window.content.height() if edge == 'bottom' else window.content.width()) < available
            drag(-30, touch=True)
            assert abs((panel.height() if edge == 'bottom' else panel.width()) - before) <= 2
            assert window.viewer is viewer and viewer._player is player and window.model is model
            assert window._current_id == original_live_id and not viewer._play_requested
            window.grab().save(str(report_dir / f'reference-map-dock-{edge}.png'))
        def map_geometry():
            return {'window': list(window.size().toTuple()), 'host': list(window._content_host.size().toTuple()),
                    'overview': list(window.content.size().toTuple()), 'panel_height': panel.height(),
                    'preferred_height': window.preferences.map_bottom_height}

        # Native window managers can limit a requested large window to the
        # monitor. Select a preference that actually fits its measured client
        # area, while still requiring the smaller window to clamp that value.
        window.resize(1500, 1120)
        settle()
        large_window_size = window.size()
        large_geometry = map_geometry()
        minimum_height = max(220, panel.minimumSizeHint().height())
        overview_minimum = max(160, window.content.minimumSizeHint().height())
        restore_target = min(640, max(minimum_height, window._content_host.height() - overview_minimum))
        window.resize(940, 650)
        settle()
        small_capacity = max(minimum_height, window._content_host.height() - 160)
        assert restore_target > small_capacity, {'large': large_geometry, 'small': map_geometry(),
                                                 'restore_target': restore_target, 'small_capacity': small_capacity}
        window.preferences.map_bottom_height = restore_target
        window._clamp_touch_map()
        settle()
        clamped_height = panel.height()
        small_geometry = map_geometry()
        assert clamped_height < restore_target and window.preferences.map_bottom_height == restore_target, small_geometry
        assert window.content.height() >= 160, small_geometry
        window.resize(large_window_size)
        settle()
        restored_height = panel.height()
        restored_geometry = map_geometry()
        assert restored_height == restore_target and window.preferences.map_bottom_height == restore_target, {
            'small': small_geometry, 'large': restored_geometry, 'restore_target': restore_target}
        assert window.content.height() >= 160, restored_geometry
        window.preferences.map_bottom_height = original['map_bottom_height']
        window._dock_touch_map('left')
        panel.location.showPopup()
        QTest.keyClick(panel.location, Qt.Key.Key_End)
        QTest.keyClick(panel.location, Qt.Key.Key_Return)
        settle()
        floating = window.touch_map_window
        assert floating is not None and floating.isVisible() and floating.windowHandle().isExposed()
        floating_frame_verified = native_frame(floating)
        floating.grab().save(str(report_dir / 'reference-map-floating.png'))
        floating.close()
        settle()
        assert window._detached and preview.isVisible() and not panel.isVisible()
        window._toggle_touch_map(True)
        settle()
        QTest.mouseClick(panel.redock_button, Qt.MouseButton.LeftButton)
        settle()
        assert panel.isVisible() and panel.edge == 'left' and not floating.isVisible()
        assert not window.preferences.map_floating
        normal_before_fullscreen = QRect(preview.normalGeometry())
        for exit_key in (Qt.Key.Key_Escape, Qt.Key.Key_F11):
            window.activateWindow()
            window.raise_()
            settle()
            button = window.fullscreen_button
            QTest.mouseClick(window.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             button.mapTo(window, button.rect().center()))
            settle()
            assert preview.isFullScreen() and not preview.isAncestorOf(panel) and not preview.isAncestorOf(window.film)
            assert not window._fullscreen_active
            assert preview.live_badge.isVisible() and preview.live_badge.width() >= preview.live_badge.sizeHint().width()
            preview.grab().save(str(report_dir / 'detached-preview-fullscreen.png'))
            preview.activateWindow()
            viewer.setFocus()
            settle()
            QTest.keyClick(preview.windowHandle(), exit_key)
            settle()
            assert preview.isMaximized() and not preview.isFullScreen() and not preview.isMinimized()
            assert preview.normalGeometry() == normal_before_fullscreen, {
                'before': normal_before_fullscreen.getRect(), 'after': preview.normalGeometry().getRect()}
        preview.showNormal()
        settle()
        preview.resize(620, 440)
        settle()
        preview.move(-30000, -20000)
        window._recover_auxiliary_windows()
        settle()
        assert any(screen.availableGeometry().intersects(preview.geometry()) for screen in QApplication.screens())
        assert QRect(0, 0, 800, 600).contains(accessible_geometry(QRect(1000, 0, 300, 300),
                                        [QRect(0, 0, 800, 600), QRect(1600, 0, 800, 600)]))
        # Store an ordinary, reachable normal rect. A native restore is allowed
        # to move a window at the extreme top edge to make its title bar usable.
        available = preview.screen().availableGeometry()
        preview.move(available.topLeft() + QPoint(40, 60))
        settle()
        preview._remember_geometry()
        remembered_normal_geometry = QRect(preview.geometry())
        preview.showMaximized()
        settle()
        preview._remember_geometry()
        window._save_preferences()
        saved = Preferences.load()
        assert saved.preview_geometry and saved.map_dock_edge == 'left'
        QTest.mouseClick(panel.return_button, Qt.MouseButton.LeftButton)
        settle()
        assert not window._detached and window.preview_window is None and window.touch_map_window is None
        assert not panel.isVisible() and window.content.currentWidget() is viewer and not window._grid_mode
        window._detach_preview()
        settle()
        assert window.preview_window.isMaximized() and not window.preview_window.isFullScreen()
        assert not window.preview_window.isMinimized()
        window.preview_window.showNormal()
        settle()
        assert window.preview_window.geometry() == remembered_normal_geometry, {
            'saved_normal_rect': remembered_normal_geometry.getRect(),
            'restored_normal_rect': window.preview_window.geometry().getRect()}
        window.preview_window.close()
        settle()
        assert not window._detached and window.content.currentWidget() is viewer
        saved = Preferences.load()
        # A separate real app instance proves main-close cleanup and ordinary
        # startup without disturbing the caller's remaining application gates.
        shutdown_probe = MainWindow()
        assert not shutdown_probe._detached and shutdown_probe.preview_window is None
        assert shutdown_probe.preferences.preview_geometry == saved.preview_geometry
        shutdown_probe.show()
        shutdown_probe.load_folder(fixtures)
        _wait(lambda: shutdown_probe._shown_image_path is not None, 25)
        shutdown_probe._detach_preview()
        shutdown_probe._toggle_touch_map(True)
        shutdown_probe._float_touch_map()
        settle()
        shutdown_probe.close()
        assert shutdown_probe.wait_for_shutdown(25)
        assert shutdown_probe.preview_window is None and shutdown_probe.touch_map_window is None
        return {'same_viewer_player_and_catalog': True, 'native_preview_and_floating_windows': True,
                'windows_native_titlebar_and_resize_frame': preview_frame_verified and floating_frame_verified,
                'preview_initially_maximized_not_fullscreen': True,
                'preview_fullscreen_button_escape_f11_restore_maximized': True,
                'preview_saved_normal_geometry_restored': True,
                'preview_normal_restore_rect': list(remembered_normal_geometry.getRect()),
                'sidebar_action_beside_folder_label_and_dynamic_text': True,
                'long_folder_path_elided_without_hiding_action': True,
                'sidebar_visibility_survives_fullscreen': True,
                'main_overview_selection_updates_preview': True, 'selection_autoplay_while_main_overview': True,
                'detach_redock_preserve_transform_and_center_without_playing': True,
                'pure_preview_client_and_fullscreen_live': True, 'virtual_touchpad_button_label': '虚拟触控板',
                'map_docks_left_right_bottom_and_floats': True, 'mouse_and_qwindow_touch_resize_overview': True,
                'independent_preferred_dimensions_and_temporary_clamp': True, 'clamped_map_height': clamped_height,
                'map_restore_preferred_height': restore_target,
                'map_large_restored_height': restored_height,
                'map_resize_actual_geometries': {'small': small_geometry, 'large': restored_geometry},
                'map_close_hides_only_and_redock_keeps_last_edge': True,
                'preview_return_and_close_restore_single_and_hide_map': True, 'main_close_removes_auxiliary_windows': True,
                'geometry_restored_and_startup_stays_normal': True, 'offscreen_and_screen_gap_recovery': True,
                'native_qt_platform': QApplication.instance().platformName(),
                'physical_multimonitor_tested': False, 'physical_touch_hardware': False}
    finally:
        if shutdown_probe is not None:
            shutdown_probe.close()
            assert shutdown_probe.wait_for_shutdown(25)
            shutdown_probe.deleteLater()
        if window._detached:
            window._redock_preview()
        for name, value in original.items():
            setattr(window.preferences, name, value)
        window.preferences.autoplay = autoplay
        window.sidebar_button.setChecked(sidebar_checked)
        window._toggle_sidebar(sidebar_checked)
        window.restoreGeometry(geometry)
        row = next((i for i, item in enumerate(model.items) if item.id == original_id), None)
        if row is not None:
            window._select_row(row, allow_autoplay=False)
        window._toggle_grid(grid_mode)
        window._save_preferences()
        settle()
