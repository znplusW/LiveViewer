import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from livegallery.cover_editor import EditorDialog
from livegallery.frame_reader import FrameCancelled, FrameReader
from livegallery.frame_tools import resolve_frame_tools
from livegallery.models import MediaItem


class FakeReader:
    instances = []

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
        self.metadata = SimpleNamespace(
            width=120, height=90,
            frames=tuple(SimpleNamespace(index=i, pts_seconds=value, duration_seconds=.04)
                         for i, value in enumerate((0.0, .04, .16, .20, .44, .48, .76, .90))),
        )
        self.instances.append(self)

    def open(self):
        self.worker_threads.append(threading.get_ident())
        return self.metadata

    def preview(self, index, edge=1200):
        self.worker_threads.append(threading.get_ident())
        self.preview_indices.append(index)
        generation = self.generation
        if index == self.block_index:
            self.entered.set()
            while not self.gate.wait(.01):
                if self.cancel_event.is_set():
                    raise FrameCancelled("closed")
        if self.generation != generation:
            raise FrameCancelled("newer frame")
        return Image.new("RGB", (120, 90), (index * 25, 30, 60))

    def export(self, index):
        self.worker_threads.append(threading.get_ident())
        self.export_indices.append(index)
        return Image.new("RGB", (120, 90), (index * 25, 30, 60))

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

    def tearDown(self):
        if self.dialog is not None:
            self.dialog._worker.close()
            self.dialog._worker.thread.join(2)
            self.dialog.close()
            self.dialog.deleteLater()
            self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
            # QTest.qWait can retain the GIL on a platform; give CPU-only fake
            # decoder/export work the same opportunity as QApplication.exec.
            time.sleep(.002)
        self.assertTrue(predicate(), "editor did not reach expected state: " + self.dialog.status_label.text())

    def open_dialog(self, **kwargs):
        self.dialog = EditorDialog(self.item, reader_factory=FakeReader, **kwargs)
        self.dialog.show()
        self.dialog.activateWindow()
        self.dialog.setFocus()
        self.wait(lambda: self.dialog.save_button.isEnabled())
        return self.dialog

    def assert_originals(self):
        self.assertEqual(self.photo.read_bytes(), self.original_photo)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def test_default_copy_real_vfr_indices_and_quality_warning(self):
        dialog = self.open_dialog()
        self.assertTrue(dialog.copy_choice.isChecked())
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

    def test_timeline_drag_keeps_only_latest_pending_decode_and_discards_old_result(self):
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
        self.assertGreater(reader.cancel_calls, 0)
        old_image = dialog._preview_image
        dialog._frame_ready(1, 0, old_image)
        self.assertEqual(dialog.selected_frame_index, 7)
        self.assertEqual(dialog._loaded_sequence, dialog._request_sequence)
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
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
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
        QTest.mouseClick(dialog.overwrite_choice, Qt.MouseButton.LeftButton,
                         pos=QPoint(10, dialog.overwrite_choice.height() // 2))
        self.assertIn("不保留备份", dialog.warning_label.text())
        self.assertIn("无法", dialog.warning_label.text())
        dialog._save()
        self.wait(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.saved_result.created_copy)
        self.assertIsNone(dialog.saved_result.backup_path)
        self.assertNotEqual(self.photo.read_bytes(), self.original_photo)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def test_failed_save_keeps_dialog_open_and_originals_unchanged(self):
        def failed(*args, **kwargs):
            raise OSError("磁盘空间不足")

        dialog = self.open_dialog(save_function=failed)
        dialog._save()
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
        dialog._save()
        self.wait(entered.is_set)
        dialog.reject()
        self.assertTrue(dialog._cancel_pending)
        self.wait(lambda: not dialog.isVisible())
        self.assertIsNone(dialog.saved_result)
        self.assert_originals()

    def test_external_mutation_during_edit_is_rejected_by_save(self):
        dialog = self.open_dialog()
        self.video.write_bytes(self.original_video + b"external change")
        dialog._save()
        self.wait(lambda: "修改" in dialog.status_label.text())
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(dialog.saved_result)
        self.assertEqual(self.photo.read_bytes(), self.original_photo)
        self.assertEqual(self.video.read_bytes(), self.original_video + b"external change")

    def test_real_vfr_video_editor_exports_selected_frame_as_valid_copy(self):
        try:
            resolve_frame_tools()
        except OSError:
            self.skipTest("frame tools unavailable in this developer environment")
        shutil.copyfile(Path(__file__).parent / "fixtures" / "VFR_PAIR.MOV", self.video)
        self.original_video = self.video.read_bytes()
        self.dialog = EditorDialog(self.item, reader_factory=FrameReader)
        self.dialog.show()
        self.wait(lambda: self.dialog.save_button.isEnabled())
        self.assertEqual(len(self.dialog.metadata.frames), 7)
        self.dialog.timeline.setValue(4)
        self.wait(lambda: self.dialog.save_button.isEnabled())
        self.assertIn("0.440 秒", self.dialog.position_label.text())
        self.dialog._save()
        self.wait(lambda: self.dialog.result() == QDialog.DialogCode.Accepted)
        result = self.dialog.saved_result
        with Image.open(result.image_path) as decoded:
            decoded.load()
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (self.dialog.metadata.width, self.dialog.metadata.height))
        self.assertEqual(result.video_path.read_bytes(), self.original_video)
        self.assert_originals()


if __name__ == "__main__":
    unittest.main()
