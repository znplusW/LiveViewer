import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QMouseEvent, QTouchEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QGroupBox

from livegallery.cover_editor import CoverTimelineSlider, EditorDialog, SaveModeDialog
from livegallery.cover_preview import PreviewSession
from livegallery.frame_reader import FrameCancelled, FrameReader
from livegallery.frame_tools import resolve_frame_tools
from livegallery.models import MediaItem


class FakeReader:
    instances = []
    frame_count = 8
    delay = 0

    def __init__(self, path, *, cancel_event=None):
        self.path = path
        self.cancel_event = cancel_event
        self.generation = 0
        self.cancel_calls = 0
        self.preview_indices = []
        self.export_indices = []
        self.worker_threads = []
        self.block_index = None
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.closed = False
        positions = (0.0, .04, .16, .20, .44, .48, .76, .90)
        positions += tuple(.90 + .04 * (index - 7) for index in range(8, self.frame_count))
        self.metadata = SimpleNamespace(
            width=120, height=90,
            frames=tuple(SimpleNamespace(index=i, pts_seconds=value, duration_seconds=.04)
                         for i, value in enumerate(positions)),
        )
        self.instances.append(self)

    def open(self):
        self.worker_threads.append(threading.get_ident())
        return self.metadata

    def preview(self, index, edge=1200):
        self.worker_threads.append(threading.get_ident())
        self.preview_indices.append(index)
        generation = self.generation
        if self.delay and self.cancel_event.wait(self.delay):
            raise FrameCancelled("closed")
        if index == self.block_index:
            self.entered.set()
            while not self.gate.wait(.01):
                if self.cancel_event.is_set():
                    raise FrameCancelled("closed")
        if self.generation != generation:
            raise FrameCancelled("newer frame")
        return Image.new("RGB", (120, 90), (index * 25 % 256, 30, 60))

    def export(self, index):
        self.worker_threads.append(threading.get_ident())
        self.export_indices.append(index)
        return Image.new("RGB", (120, 90), (index * 25 % 256, 30, 60))

    def cancel_current(self):
        self.generation += 1
        self.cancel_calls += 1

    def close(self):
        self.closed = True
        self.gate.set()


class CoverEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.photo = self.folder / "Holiday.jpg"
        self.video = self.folder / "Holiday.MOV"
        Image.new("RGB", (200, 150), "blue").save(self.photo, "JPEG")
        self.video.write_bytes(b"video source bytes\x00" * 500)
        self.original_photo = self.photo.read_bytes()
        self.original_video = self.video.read_bytes()
        self.item = MediaItem("live", self.photo, self.video)
        self.dialog = None
        FakeReader.instances.clear()
        FakeReader.frame_count = 8
        FakeReader.delay = 0

    def tearDown(self):
        if self.dialog is not None:
            self.dialog._worker.close()
            self.dialog._worker.thread.join(2)
            self.dialog.close()
            self.dialog.deleteLater()
            self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
            time.sleep(.002)
        self.assertTrue(predicate(), "editor did not reach expected state: " + self.dialog.status_label.text())

    def open_dialog(self, **kwargs):
        self.dialog = EditorDialog(self.item, reader_factory=FakeReader, **kwargs)
        self.dialog.show()
        self.dialog.activateWindow()
        self.dialog.setFocus()
        self.wait(lambda: self.dialog.save_button.isEnabled())
        return self.dialog

    def choose_save(self, mode="copy", *, cancel=False, inspect=None):
        captured = []
        def choose():
            prompt = QApplication.activeModalWidget()
            if not isinstance(prompt, SaveModeDialog):
                QTimer.singleShot(5, choose)
                return
            captured.append(prompt)
            if inspect:
                inspect(prompt)
            if cancel:
                prompt.reject()
            else:
                prompt.overwrite_choice.setChecked(mode == "overwrite")
                prompt.copy_choice.setChecked(mode == "copy")
                prompt.accept()
        QTimer.singleShot(0, choose)
        self.dialog._save()
        self.assertEqual(len(captured), 1)

    def assert_originals(self):
        self.assertEqual(self.photo.read_bytes(), self.original_photo)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def test_main_editor_has_larger_preview_maximize_and_no_save_mode_panel(self):
        dialog = self.open_dialog()
        self.assertEqual(dialog.windowTitle(), "更改封面图 — LiveViewer")
        self.assertEqual(dialog.save_button.text(), "保存封面图")
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        self.assertFalse(dialog.findChildren(QGroupBox))
        self.assertFalse(hasattr(dialog, "copy_choice"))
        self.assertGreater(dialog.preview.height(), 440)
        before_size = dialog.size()
        before_frame = dialog.selected_frame_index
        dialog.showMaximized()
        self.wait(dialog.isMaximized)
        dialog.showNormal()
        self.wait(lambda: not dialog.isMaximized())
        self.assertEqual(dialog.size(), before_size)
        self.assertEqual(dialog.selected_frame_index, before_frame)
        self.assert_originals()

    def test_initial_window_fits_scaled_small_desktop(self):
        screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 960, 540))
        with patch.object(EditorDialog, "screen", return_value=screen):
            dialog = self.open_dialog()
        self.assertLessEqual(dialog.height(), 470)
        self.assertLessEqual(dialog.minimumSizeHint().height(), dialog.height())
        self.assertLessEqual(dialog.width(), 900)
        self.assertTrue(dialog.save_button.isVisible())

    def test_real_vfr_indices_and_quality_warning(self):
        dialog = self.open_dialog()
        self.assertIn("120 × 90", dialog.resolution_label.text())
        self.assertIn("分辨率可能低于原照片", dialog.resolution_label.text())
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        self.wait(lambda: dialog.selected_frame_index == 1 and dialog.save_button.isEnabled())
        self.assertIn("0.040 秒", dialog.position_label.text())
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        self.wait(lambda: dialog.selected_frame_index == 2 and dialog.save_button.isEnabled())
        self.assertIn("0.160 秒", dialog.position_label.text())
        QTest.keyClick(dialog, Qt.Key.Key_Left)
        self.wait(lambda: dialog.selected_frame_index == 1 and dialog.save_button.isEnabled())
        self.assertEqual(FakeReader.instances[0].preview_indices, [0, 1, 2, 1])
        self.assertTrue(all(value != threading.get_ident() for value in FakeReader.instances[0].worker_threads))
        self.assert_originals()

    def test_pending_navigation_does_not_cancel_active_preview_or_allow_stale_save(self):
        dialog = self.open_dialog()
        reader = FakeReader.instances[0]
        reader.block_index = 1
        dialog.timeline.setValue(1)
        self.wait(reader.entered.is_set)
        for index in range(2, 8):
            dialog.timeline.setValue(index)
        self.assertFalse(dialog.save_button.isEnabled())
        reader.gate.set()
        self.wait(lambda: dialog.selected_frame_index == 7 and dialog.save_button.isEnabled())
        self.assertEqual(reader.preview_indices, [0, 1, 7])
        self.assertEqual(reader.cancel_calls, 0)
        old_image = dialog._preview_image
        dialog._frame_ready(1, 0, old_image)
        self.assertEqual(dialog.displayed_frame_index, 7)
        self.assertEqual(dialog._loaded_sequence, dialog._request_sequence)
        self.assert_originals()

    def test_held_mouse_drag_updates_previews_before_button_release(self):
        FakeReader.frame_count = 80
        FakeReader.delay = .035
        dialog = self.open_dialog()
        slider = dialog.timeline
        _, groove, handle = slider._geometry()
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=handle.center())
        shown = set()
        last_position = handle.center()
        try:
            for index in range(1, 45):
                x = groove.x() + handle.width() / 2 + (groove.width() - handle.width()) * index / slider.maximum()
                last_position = QPoint(round(x), handle.center().y())
                event = QMouseEvent(QEvent.Type.MouseMove, QPointF(last_position),
                                    QPointF(slider.mapToGlobal(last_position)), Qt.MouseButton.NoButton,
                                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                QApplication.sendEvent(slider, event)
                QTest.qWait(5)
                time.sleep(.001)
                shown.add(dialog.displayed_frame_index)
                self.assertTrue(slider.isSliderDown())
            self.assertGreater(len(shown - {None, 0}), 2, "previews stalled until mouse release")
            self.assertGreater(dialog.selected_frame_index, 30)
            self.assertEqual(FakeReader.instances[0].cancel_calls, 0)
        finally:
            QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=last_position)
        self.wait(lambda: dialog.displayed_frame_index == dialog.selected_frame_index and dialog.save_button.isEnabled())
        self.assert_originals()

    def test_arrow_auto_repeat_updates_previews_before_key_release(self):
        FakeReader.frame_count = 80
        FakeReader.delay = .035
        dialog = self.open_dialog()
        QTest.keyPress(dialog, Qt.Key.Key_Right)
        shown = set()
        try:
            for _ in range(40):
                repeat = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                                   Qt.KeyboardModifier.NoModifier, "", True, 1)
                QApplication.sendEvent(dialog, repeat)
                QTest.qWait(5)
                time.sleep(.001)
                shown.add(dialog.displayed_frame_index)
            self.assertGreater(dialog.selected_frame_index, 30)
            self.assertGreater(len(shown - {None, 0}), 2, "previews stalled until key release")
            self.assertEqual(FakeReader.instances[0].cancel_calls, 0)
        finally:
            QTest.keyRelease(dialog, Qt.Key.Key_Right)
        self.wait(lambda: dialog.displayed_frame_index == dialog.selected_frame_index and dialog.save_button.isEnabled())
        self.assert_originals()

    def test_real_preview_session_cold_fallback_updates_during_held_auto_repeat(self):
        # Exercise the actual PreviewSession scheduler and its callbacks, with
        # only media decoding substituted. This must cover the normal session
        # path, not just the editor's direct-reader testing fallback.
        class DemandOnlySession(PreviewSession):
            def _preload(self):
                with self._condition:
                    self._stream_done = True
                    self._condition.notify_all()

            def _reader(self):
                if self._request_reader is None:
                    self._request_reader = FakeReader(self.path, cancel_event=self._closed)
                return self._request_reader

        FakeReader.frame_count = 80
        FakeReader.delay = .035
        dialog = self.open_dialog(preview_factory=DemandOnlySession)
        session = dialog._worker._preview
        QTest.keyPress(dialog, Qt.Key.Key_Right)
        shown = set()
        try:
            for _ in range(50):
                repeat = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                                   Qt.KeyboardModifier.NoModifier, "", True, 1)
                QApplication.sendEvent(dialog, repeat)
                QTest.qWait(5)
                time.sleep(.001)
                shown.add(dialog.displayed_frame_index)
                if dialog.displayed_frame_index != dialog.selected_frame_index:
                    self.assertFalse(dialog.save_button.isEnabled())
            self.assertGreater(dialog.selected_frame_index, 40)
            self.assertGreater(len(shown - {None, 0}), 2, "normal preview-session path starved until release")
            self.assertEqual(session._request_reader.cancel_calls, 0)
        finally:
            QTest.keyRelease(dialog, Qt.Key.Key_Right)
        self.wait(lambda: dialog.displayed_frame_index == dialog.selected_frame_index and dialog.save_button.isEnabled())
        self.assert_originals()

    def test_cached_session_selection_is_immediate_and_forwards_request_for_refinement(self):
        class CachedSession(PreviewSession):
            def __init__(self, *args, **kwargs):
                self.requested_indices = []
                super().__init__(*args, **kwargs)

            def _preload(self):
                for index in range(len(self.metadata.frames)):
                    self._store(index, Image.new("RGB", (120, 90), (index * 25, 30, 60)))
                with self._condition:
                    self._stream_done = True
                    self._condition.notify_all()

            def request(self, index):
                self.requested_indices.append(index)
                super().request(index)

        dialog = self.open_dialog(preview_factory=CachedSession)
        session = dialog._worker._preview
        self.wait(lambda: session.preload_complete)
        dialog.timeline.setValue(4)
        self.assertEqual(dialog.displayed_frame_index, 4)
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertEqual(session.requested_indices[-1], 4)
        self.assertEqual(FakeReader.instances[0].preview_indices, [])
        self.assert_originals()

    def test_original_cover_marker_stays_fixed_while_selected_frame_changes(self):
        dialog = self.open_dialog()
        position = SimpleNamespace(index=3, seconds=.20, approximate=True, confidence=.8)
        dialog._position_ready(position)
        self.assertEqual(dialog.timeline.original_index, 3)
        self.assertIn("原封面图", dialog.original_label.text())
        self.assertIn("0.200 秒", dialog.original_label.text())
        self.assertIn("近似", dialog.original_label.text())
        dialog.timeline.setValue(6)
        self.wait(lambda: dialog.save_button.isEnabled())
        self.assertEqual(dialog.timeline.original_index, 3)
        self.assertEqual(dialog.selected_frame_index, 6)
        dialog.grab()  # Paint the fixed marker in its actual slider geometry.

    def test_save_prompt_defaults_copy_and_cancel_returns_to_same_frame(self):
        dialog = self.open_dialog()
        dialog.timeline.setValue(4)
        self.wait(lambda: dialog.save_button.isEnabled())
        def inspect(prompt):
            self.assertTrue(prompt.copy_choice.isChecked())
            self.assertEqual(prompt.mode, "copy")
            self.assertIn("副本", prompt.warning_label.text())
            self.assertEqual(prompt.windowTitle(), "保存封面图")
        self.choose_save(cancel=True, inspect=inspect)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.selected_frame_index, 4)
        self.assertEqual(dialog.displayed_frame_index, 4)
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertIsNone(dialog.saved_result)
        self.assertEqual(FakeReader.instances[0].export_indices, [])
        self.assert_originals()

    def test_cancel_before_save_makes_no_changes(self):
        dialog = self.open_dialog()
        dialog.timeline.setValue(3)
        dialog.reject()
        self.wait(lambda: not dialog.isVisible())
        self.assertIsNone(dialog.saved_result)
        self.assert_originals()
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["Holiday.MOV", "Holiday.jpg"])

    def test_successful_copy_exports_selected_full_frame_and_returns_pair(self):
        dialog = self.open_dialog()
        dialog.timeline.setValue(4)
        self.wait(lambda: dialog.save_button.isEnabled())
        self.choose_save()
        self.wait(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        result = dialog.saved_result
        self.assertTrue(result.created_copy)
        self.assertEqual(result.image_path.name, "Holiday v1.jpg")
        self.assertEqual(result.video_path.name, "Holiday v1.MOV")
        self.assertEqual(result.video_path.read_bytes(), self.original_video)
        self.assertEqual(FakeReader.instances[0].export_indices, [4])
        with Image.open(result.image_path) as image:
            self.assertEqual(image.size, (120, 90))
        self.assert_originals()

    def test_overwrite_warning_and_no_backup_save(self):
        dialog = self.open_dialog()
        def inspect(prompt):
            prompt.overwrite_choice.setChecked(True)
            self.assertIn("不保留备份", prompt.warning_label.text())
            self.assertIn("无法", prompt.warning_label.text())
            self.assertIn("校验通过", prompt.warning_label.text())
        self.choose_save("overwrite", inspect=inspect)
        self.wait(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.saved_result.created_copy)
        self.assertIsNone(dialog.saved_result.backup_path)
        self.assertNotEqual(self.photo.read_bytes(), self.original_photo)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def test_failed_save_keeps_dialog_open_and_originals_unchanged(self):
        def failed(*args, **kwargs):
            raise OSError("磁盘空间不足")
        dialog = self.open_dialog(save_function=failed)
        self.choose_save()
        self.wait(lambda: "磁盘空间不足" in dialog.status_label.text())
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertIsNone(dialog.saved_result)
        self.assert_originals()

    def test_cancel_during_save_waits_for_worker_cleanup(self):
        entered = threading.Event()
        def saving(*args, cancel_event=None, **kwargs):
            entered.set()
            if not cancel_event.wait(2):
                raise AssertionError("save cancel not delivered")
            raise InterruptedError("cancelled")
        dialog = self.open_dialog(save_function=saving)
        self.choose_save()
        self.wait(entered.is_set)
        dialog.reject()
        self.assertTrue(dialog._cancel_pending)
        self.wait(lambda: not dialog.isVisible())
        self.assertIsNone(dialog.saved_result)
        self.assert_originals()

    def test_external_mutation_during_edit_is_rejected_by_save(self):
        dialog = self.open_dialog()
        self.video.write_bytes(self.original_video + b"external change")
        self.choose_save()
        self.wait(lambda: "修改" in dialog.status_label.text())
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(dialog.saved_result)
        self.assertEqual(self.photo.read_bytes(), self.original_photo)
        self.assertEqual(self.video.read_bytes(), self.original_video + b"external change")

    def test_real_session_original_marker_and_cached_drag_update_before_release(self):
        try:
            resolve_frame_tools()
        except OSError:
            self.skipTest("frame tools unavailable in this developer environment")
        shutil.copyfile(Path(__file__).parent / "fixtures" / "VFR_PAIR.MOV", self.video)
        with FrameReader(self.video) as reader:
            reader.export(4).save(self.photo, "JPEG", quality=95)
        self.original_photo = self.photo.read_bytes()
        self.original_video = self.video.read_bytes()
        self.dialog = EditorDialog(self.item)
        self.dialog.show()
        self.wait(lambda: self.dialog.save_button.isEnabled(), timeout=6)
        self.wait(lambda: self.dialog.original_position is not None, timeout=6)
        self.assertEqual(self.dialog.original_position.index, 4)
        self.assertEqual(self.dialog.timeline.original_index, 4)
        self.assertIn("0.440 秒", self.dialog.original_label.text())
        session = self.dialog._worker._preview
        self.wait(lambda: session.preload_complete)
        slider = self.dialog.timeline
        _, groove, handle = slider._geometry()
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=handle.center())
        shown = set()
        last_position = handle.center()
        try:
            for index in range(1, 7):
                x = groove.x() + handle.width() / 2 + (groove.width() - handle.width()) * index / slider.maximum()
                last_position = QPoint(round(x), handle.center().y())
                QApplication.sendEvent(slider, QMouseEvent(
                    QEvent.Type.MouseMove, QPointF(last_position), QPointF(slider.mapToGlobal(last_position)),
                    Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
                QTest.qWait(8)
                shown.add(self.dialog.displayed_frame_index)
                self.assertTrue(slider.isSliderDown())
                self.assertEqual(self.dialog.displayed_frame_index, self.dialog.selected_frame_index)
            self.assertGreater(len(shown), 3)
        finally:
            QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=last_position)
        # Now exercise the native Qt touch route on the actual cached decoder,
        # not Qt's fallback synthesized mouse events.
        slider.setValue(0)
        self.wait(lambda: self.dialog.displayed_frame_index == 0)
        device = QTest.createTouchDevice()
        touch = QTest.touchEvent(slider, device, False)
        _, groove, handle = slider._geometry()
        touch.press(0, handle.center(), slider).commit()
        self.app.processEvents()
        touch_frames = set()
        last_position = handle.center()
        for index in (1, 2, 4, 6):
            x = groove.x() + handle.width() / 2 + (groove.width() - handle.width()) * index / slider.maximum()
            last_position = QPoint(round(x), handle.center().y())
            touch.move(0, last_position, slider).commit()
            self.app.processEvents()
            self.wait(lambda: self.dialog.selected_frame_index == index and self.dialog.displayed_frame_index == index)
            self.assertTrue(slider.isSliderDown(), "finger ended before preview update")
            touch_frames.add(self.dialog.displayed_frame_index)
        self.assertGreater(len(touch_frames), 3)
        touch.release(0, last_position, slider).commit()
        self.app.processEvents()
        self.assertFalse(slider.isSliderDown())
        self.choose_save(cancel=True)
        self.assertEqual(slider.original_index, 4)
        self.assert_originals()

    def test_real_vfr_video_editor_exports_selected_frame_as_valid_copy(self):
        try:
            resolve_frame_tools()
        except OSError:
            self.skipTest("frame tools unavailable in this developer environment")
        shutil.copyfile(Path(__file__).parent / "fixtures" / "VFR_PAIR.MOV", self.video)
        self.original_video = self.video.read_bytes()
        self.dialog = EditorDialog(self.item, reader_factory=FrameReader, preview_factory=False)
        self.dialog.show()
        self.wait(lambda: self.dialog.save_button.isEnabled())
        self.assertEqual(len(self.dialog.metadata.frames), 7)
        self.dialog.timeline.setValue(4)
        self.wait(lambda: self.dialog.save_button.isEnabled())
        self.assertIn("0.440 秒", self.dialog.position_label.text())
        self.choose_save()
        self.wait(lambda: self.dialog.result() == QDialog.DialogCode.Accepted)
        result = self.dialog.saved_result
        with Image.open(result.image_path) as decoded:
            decoded.load()
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (self.dialog.metadata.width, self.dialog.metadata.height))
        self.assertEqual(result.video_path.read_bytes(), self.original_video)
        self.assert_originals()


class CoverTimelineTouchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.slider = CoverTimelineSlider()
        self.slider.setRange(0, 99)
        self.slider.resize(500, 60)
        self.slider.show()
        self.slider.activateWindow()
        self.slider.setFocus()
        self.app.processEvents()
        self.device = QTest.createTouchDevice()
        self.sequence = QTest.touchEvent(self.slider, self.device, False)

    def tearDown(self):
        QApplication.sendEvent(self.slider, QTouchEvent(QEvent.Type.TouchCancel, self.device))
        self.slider.close()
        self.slider.deleteLater()
        self.app.processEvents()

    def position(self, value):
        option, groove, handle = self.slider._geometry()
        horizontal = self.slider.orientation() == Qt.Orientation.Horizontal
        length = (groove.width() - handle.width()) if horizontal else (groove.height() - handle.height())
        fraction = value / self.slider.maximum()
        if option.upsideDown:
            fraction = 1 - fraction
        if horizontal:
            return QPoint(round(groove.x() + handle.width() / 2 + length * fraction), handle.center().y())
        return QPoint(handle.center().x(), round(groove.y() + handle.height() / 2 + length * fraction))

    def touch(self, method, position):
        getattr(self.sequence, method)(0, position, self.slider).commit()
        self.app.processEvents()

    def synthetic_click(self, position):
        for kind, button, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                                      (QEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
            event = QMouseEvent(kind, QPointF(position), QPointF(position),
                                QPointF(self.slider.mapToGlobal(position)), button, buttons,
                                Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedByQt)
            QApplication.sendEvent(self.slider, event)
        self.app.processEvents()

    def test_touch_updates_value_while_contact_is_down_and_retains_final_end_position(self):
        self.touch("press", self.position(10))
        self.assertTrue(self.slider.isSliderDown())
        self.assertAlmostEqual(self.slider.value(), 10, delta=1)
        for value in (25, 50, 75):
            self.touch("move", self.position(value))
            self.assertTrue(self.slider.isSliderDown())
            self.assertAlmostEqual(self.slider.value(), value, delta=1)
        self.touch("release", self.position(85))
        self.assertFalse(self.slider.isSliderDown())
        self.assertAlmostEqual(self.slider.value(), 85, delta=1)
        self.synthetic_click(self.position(5))
        self.assertAlmostEqual(self.slider.value(), 85, delta=1)
        QTest.mouseClick(self.slider, Qt.MouseButton.LeftButton, pos=self.position(15))
        self.assertAlmostEqual(self.slider.value(), 15, delta=1)

    def test_touch_cancel_rejects_late_updates_and_real_mouse_takes_over_immediately(self):
        self.touch("press", self.position(20))
        self.touch("move", self.position(40))
        QApplication.sendEvent(self.slider, QTouchEvent(QEvent.Type.TouchCancel, self.device))
        self.assertFalse(self.slider.isSliderDown())
        value = self.slider.value()
        self.touch("move", self.position(90))
        self.touch("release", self.position(95))
        self.assertEqual(self.slider.value(), value)
        self.synthetic_click(self.position(0))
        self.assertEqual(self.slider.value(), value)
        QTest.mouseClick(self.slider, Qt.MouseButton.LeftButton, pos=self.position(65))
        self.assertAlmostEqual(self.slider.value(), 65, delta=1)
        self.touch("press", self.position(30))
        QTest.mouseClick(self.slider, Qt.MouseButton.LeftButton, pos=self.position(70))
        self.assertFalse(self.slider.isSliderDown())
        self.assertAlmostEqual(self.slider.value(), 70, delta=1)
        self.touch("release", self.position(5))
        self.assertAlmostEqual(self.slider.value(), 70, delta=1)

    def test_touch_mapping_handles_resize_vertical_inversion_and_logical_dpi(self):
        for orientation, size in ((Qt.Orientation.Horizontal, (360, 50)),
                                  (Qt.Orientation.Horizontal, (790, 60)),
                                  (Qt.Orientation.Vertical, (60, 430))):
            self.slider.setOrientation(orientation)
            self.slider.resize(*size)
            self.app.processEvents()
            for inverted in (False, True):
                self.slider.setInvertedAppearance(inverted)
                with patch.object(self.slider, "devicePixelRatioF", return_value=2.0):
                    self.touch("press", self.position(15))
                    self.touch("move", self.position(85))
                    self.assertTrue(self.slider.isSliderDown())
                    self.assertAlmostEqual(self.slider.value(), 85, delta=1)
                    self.touch("release", self.position(50))
                    self.assertAlmostEqual(self.slider.value(), 50, delta=1)
                self.slider.set_original_index(35)
                self.slider.grab()
        self.slider.setOrientation(Qt.Orientation.Horizontal)
        self.slider.setInvertedAppearance(False)
        self.slider.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.app.processEvents()
        self.touch("press", self.position(20))
        self.touch("release", self.position(80))
        self.assertAlmostEqual(self.slider.value(), 80, delta=1)

    def test_touch_clamps_at_ends_and_focus_loss_cancels_future_movement(self):
        self.touch("press", self.position(40))
        self.touch("move", QPoint(-300, self.slider.height() // 2))
        self.assertEqual(self.slider.value(), 0)
        self.touch("move", QPoint(self.slider.width() + 300, self.slider.height() // 2))
        self.assertEqual(self.slider.value(), 99)
        QApplication.sendEvent(self.slider, QEvent(QEvent.Type.WindowDeactivate))
        self.assertFalse(self.slider.isSliderDown())
        self.touch("release", self.position(0))
        self.assertEqual(self.slider.value(), 99)



if __name__ == "__main__":
    unittest.main()
