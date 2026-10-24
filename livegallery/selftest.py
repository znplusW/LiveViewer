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

from PySide6.QtCore import QCoreApplication, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

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


def _check_heic_cover_roundtrip(fixtures: Path, report_dir: Path):
    from .cover_save import CoverSource, save_cover
    with tempfile.TemporaryDirectory(prefix='heic-cover-', dir=report_dir) as temporary:
        folder = Path(temporary)
        still, video = folder / 'Real_HEIC.heic', folder / 'Real_HEIC.MOV'
        shutil.copy2(fixtures / 'Live_H264.heic', still)
        shutil.copy2(fixtures / 'Live_H264.MOV', video)
        item = MediaItem('heic-roundtrip', still, video)
        before_image, before_video = _file_digest(still), _file_digest(video)
        with FrameReader(video) as reader:
            selected = reader.export(0)
        copied = save_cover(CoverSource.capture(item), selected, 'copy')
        assert copied.image_path.suffix == '.heic' and copied.image_path.stem == copied.video_path.stem
        assert _file_digest(still) == before_image and _file_digest(video) == before_video
        assert _file_digest(copied.video_path) == before_video
        replaced = save_cover(CoverSource.capture(item), selected, 'overwrite')
        assert replaced.backup_path is None and _file_digest(video) == before_video
        assert _file_digest(still) != before_image
        for path in (copied.image_path, replaced.image_path):
            header = path.read_bytes()[:128]
            assert header[4:8] == b'ftyp' and any(brand in header[8:] for brand in (b'heic', b'heix', b'hevc'))
            decoded = load_image(path, None)
            assert decoded.size == selected.size, 'HEIC roundtrip changed full-resolution dimensions'
            decoded.load()
        return {'format': 'HEVC HEIC', 'size': list(selected.size),
                'copy_and_overwrite': True, 'video_bytes_unchanged': True}


def _check_real_cover_editor(fixtures: Path, report_dir: Path, parent):
    from .cover_editor import EditorDialog, SaveModeDialog
    with tempfile.TemporaryDirectory(prefix='editor-vfr-', dir=report_dir) as temporary:
        folder = Path(temporary)
        still, video = folder / 'Editor_VFR.jpg', folder / 'Editor_VFR.MOV'
        shutil.copy2(fixtures / 'VFR_PAIR.jpg', still)
        shutil.copy2(fixtures / 'VFR_PAIR.MOV', video)
        item = MediaItem('vfr-editor', still, video)
        before_image, before_video = _file_digest(still), _file_digest(video)
        dialog = EditorDialog(item, parent)
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
            device = QTest.createTouchDevice()
            touch = QTest.touchEvent(slider, device, False)
            last = handle.center()
            touch.press(0, last, slider).commit()
            QCoreApplication.processEvents()
            shown_while_pressed = []
            try:
                assert slider.isSliderDown(), 'Touch begin did not capture the cover timeline'
                for index in (1, 2, 4):
                    last = slider_position(index)
                    touch.move(0, last, slider).commit()
                    _wait(lambda index=index: dialog.selected_frame_index == index
                          and dialog.displayed_frame_index == index and dialog.save_button.isEnabled(), 8)
                    assert slider.isSliderDown(), 'Finger was released before preview changed'
                    shown_while_pressed.append(dialog.displayed_frame_index)
            finally:
                touch.release(0, last, slider).commit()
                QCoreApplication.processEvents()
            assert shown_while_pressed == [1, 2, 4] and not slider.isSliderDown()
            _wait(lambda: dialog.selected_frame_index == 4 and dialog.save_button.isEnabled(), 20)
            assert abs(dialog.metadata.frames[4].pts_seconds - .44) < .000001
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
            assert _file_digest(saved.video_path) == before_video
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
                'copy_preserves_originals_and_video_bytes': True, 'cancel_no_changes': True,
                'save_mode_prompt_after_save': True, 'maximize_button_available': True,
                'original_cover_marker_frame': 3,
                'touch_scrub_updates_before_release': shown_while_pressed}


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


def _check_settings_controls(settings, window, report_dir: Path):
    """Send real wheel/key/touch events through the shipped settings dialog."""
    from PySide6.QtGui import QAccessible
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
               (settings.mode, settings.zoom_mode, settings.horizontal_scroll_mode, settings.cache_location)]
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
    position = QPoint(20, control.height() // 2)
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=position)
    assert control.isChecked() != before, 'Pill switch did not respond to a click'
    QTest.keyClick(control, Qt.Key.Key_Space)
    assert control.isChecked() == before, 'Pill switch did not respond to Space'
    device = QTest.createTouchDevice()
    touch = QTest.touchEvent(control, device, False)
    touch.press(0, position, control).commit()
    touch.release(0, position, control).commit()
    QCoreApplication.processEvents()
    assert control.isChecked() != before, 'Pill switch did not respond to simulated touch'
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
            'focused_controls_and_spinbox_children_protected': True,
            'angle_and_pixel_scroll_page_once': True, 'opened_dropdown_selectable': True,
            'checkbox_accessibility_preserved': True, 'click_space_touch_toggle': True,
            'disabled_switch_unchanged': True, 'overview_filter_remains_checkboxes': True}


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
        library_cache = _check_library_cache(fixtures, report_dir)
        report['checks'].append({'check': 'cache_locations_portable_library_reuse_and_validation',
                                 **library_cache, 'status': 'passed'})
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
        editor_result = _check_real_cover_editor(fixtures, report_dir, window)
        report['checks'].append({'check': 'real_vfr_cover_editor_copy_and_cancel',
                                 **editor_result, 'status': 'passed'})
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
