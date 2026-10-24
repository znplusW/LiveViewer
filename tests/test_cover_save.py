import errno
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from livegallery import cover_save
from livegallery.cover_save import CoverSaveError, CoverSource, SourceChangedError, WORK_DIRECTORY, save_cover
from livegallery.models import MediaItem


class CoverSaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.photo = self.folder / "Holiday.JPEG"
        self.video = self.folder / "Holiday.MOV"
        Image.new("RGB", (80, 60), "blue").save(self.photo, "JPEG")
        self.video.write_bytes(b"original-video-data\x00" * 10_000)
        self.original_image = self.photo.read_bytes()
        self.original_video = self.video.read_bytes()
        self.item = MediaItem("photo", self.photo, self.video)
        self.source = CoverSource.capture(self.item)
        self.frame = Image.new("RGB", (120, 90), "red")

    def tearDown(self):
        self.photo.chmod(0o600)
        self.folder.chmod(0o700)
        self.directory.cleanup()

    def assert_originals(self):
        self.assertEqual(self.photo.read_bytes(), self.original_image)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def assert_no_pending_files(self):
        work = self.folder / WORK_DIRECTORY
        self.assertTrue(not work.exists() or not list(work.iterdir()))

    def test_copy_pair_shares_version_preserves_extension_and_video_bytes(self):
        result = save_cover(self.source, self.frame)
        self.assertEqual(result.image_path.name, "Holiday v1.JPEG")
        self.assertEqual(result.video_path.name, "Holiday v1.MOV")
        self.assertTrue(result.created_copy)
        self.assertIsNone(result.backup_path)
        with Image.open(result.image_path) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, self.frame.size)
        self.assertEqual(result.video_path.read_bytes(), self.original_video)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_overwrite_is_image_only_atomic_and_keeps_no_backup(self):
        original_replace = cover_save.os.replace
        observations = []

        def replace(staged, target):
            observations.append((self.photo.read_bytes(), Path(staged).read_bytes()[:3]))
            return original_replace(staged, target)

        with patch.object(cover_save.os, "replace", side_effect=replace):
            result = save_cover(self.source, self.frame, "overwrite")
        self.assertEqual(observations, [(self.original_image, b"\xff\xd8\xff")])
        self.assertEqual(result.image_path, self.photo)
        self.assertFalse(result.created_copy)
        self.assertIsNone(result.backup_path)
        self.assertEqual(self.video.read_bytes(), self.original_video)
        with Image.open(self.photo) as image:
            self.assertEqual(image.size, self.frame.size)
        self.assert_no_pending_files()
        self.assertFalse((self.folder / ".LiveViewer-backups").exists())

    def test_case_insensitive_collision_any_extension_and_literal_version_stem(self):
        (self.folder / "HOLIDAY V1.PNG").write_bytes(b"existing")
        result = save_cover(self.source, self.frame)
        self.assertEqual(result.image_path.stem, "Holiday v2")
        second = CoverSource.capture(MediaItem("copy", result.image_path, result.video_path))
        nested = save_cover(second, self.frame)
        self.assertEqual(nested.image_path.stem, "Holiday v2 v1")
        self.assertEqual(nested.video_path.stem, nested.image_path.stem)
        self.assert_originals()

    def test_copy_preserves_each_original_stem_case_with_shared_version_number(self):
        lower_video = self.folder / "holiday.MOV"
        self.video.rename(lower_video)
        self.video = lower_video
        self.item = MediaItem("photo", self.photo, self.video)
        self.source = CoverSource.capture(self.item)
        result = save_cover(self.source, self.frame)
        self.assertEqual(result.image_path.name, "Holiday v1.JPEG")
        self.assertEqual(result.video_path.name, "holiday v1.MOV")
        self.assertEqual(result.image_path.stem.casefold(), result.video_path.stem.casefold())
        self.assert_originals()

    def test_source_mutation_since_edit_open_is_rejected(self):
        self.video.write_bytes(self.original_video + b"changed")
        with self.assertRaises(SourceChangedError):
            save_cover(self.source, self.frame, "overwrite")
        self.assertEqual(self.photo.read_bytes(), self.original_image)
        self.assert_no_pending_files()

    def test_source_mutation_during_encode_is_rejected_before_commit(self):
        encode = cover_save._encode_image

        def mutate(*args):
            encode(*args)
            self.video.write_bytes(self.original_video + b"changed")

        with patch.object(cover_save, "_encode_image", side_effect=mutate):
            with self.assertRaises(SourceChangedError):
                save_cover(self.source, self.frame, "overwrite")
        self.assertEqual(self.photo.read_bytes(), self.original_image)
        self.assert_no_pending_files()

    def test_failure_publishing_second_file_rolls_back_first(self):
        publish = cover_save._publish_no_replace
        calls = []

        def fail_second(staged, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, "disk full")
            publish(staged, target)

        with patch.object(cover_save, "_publish_no_replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                save_cover(self.source, self.frame)
        self.assertFalse(any(self.folder.glob("Holiday v1.*")))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_concurrent_creator_is_not_overwritten(self):
        publish = cover_save._publish_no_replace

        def race(staged, target):
            Path(target).write_bytes(b"created by another program")
            publish(staged, target)

        with patch.object(cover_save, "_publish_no_replace", side_effect=race):
            with self.assertRaises(FileExistsError):
                save_cover(self.source, self.frame)
        self.assertEqual((self.folder / "Holiday v1.JPEG").read_bytes(), b"created by another program")
        self.assertFalse((self.folder / "Holiday v1.MOV").exists())
        self.assert_originals()
        self.assert_no_pending_files()

    def test_concurrent_other_format_same_stem_rolls_back_owned_output(self):
        publish = cover_save._publish_no_replace

        def race(staged, target):
            publish(staged, target)
            (self.folder / "HOLIDAY V1.HEIC").write_bytes(b"other creator")

        with patch.object(cover_save, "_publish_no_replace", side_effect=race):
            with self.assertRaises(FileExistsError):
                save_cover(self.source, self.frame)
        self.assertFalse((self.folder / "Holiday v1.JPEG").exists())
        self.assertEqual((self.folder / "HOLIDAY V1.HEIC").read_bytes(), b"other creator")
        self.assert_originals()
        self.assert_no_pending_files()

    def test_cancel_after_first_publication_rolls_back_pair(self):
        cancel = threading.Event()
        publish = cover_save._publish_no_replace

        def cancelled(staged, target):
            publish(staged, target)
            cancel.set()

        with patch.object(cover_save, "_publish_no_replace", side_effect=cancelled):
            with self.assertRaises(InterruptedError):
                save_cover(self.source, self.frame, cancel_event=cancel)
        self.assertFalse(any(self.folder.glob("Holiday v1.*")))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_disk_full_or_encoder_failure_cleans_staging(self):
        def fail(image, target, *_args):
            Path(target).write_bytes(b"partial image")
            raise OSError(errno.ENOSPC, "disk full")

        with patch.object(cover_save, "_encode_image", side_effect=fail):
            with self.assertRaises(OSError):
                save_cover(self.source, self.frame, "overwrite")
        self.assert_originals()
        self.assert_no_pending_files()

    def test_read_only_original_is_not_replaced(self):
        self.photo.chmod(0o444)
        with self.assertRaises(CoverSaveError):
            save_cover(self.source, self.frame, "overwrite")
        self.assert_originals()
        self.assert_no_pending_files()

    def test_cancel_before_saving_does_not_create_outputs(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            save_cover(self.source, self.frame, cancel_event=cancel)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_heic_cannot_be_saved_as_disguised_jpeg(self):
        heic = self.folder / "Holiday.HEIC"
        heic.write_bytes(b"original heic content")
        source = CoverSource.capture(MediaItem("heic", heic, self.video))

        def fake_heic(image, target, **kwargs):
            image.save(target, "JPEG")

        for mode in ("copy", "overwrite"):
            with self.assertRaises(CoverSaveError):
                save_cover(source, self.frame, mode, heic_encoder=fake_heic)
            self.assertEqual(heic.read_bytes(), b"original heic content")
            self.assert_no_pending_files()
        self.assert_originals()

    def test_reserved_work_directory_symlink_is_rejected(self):
        outside = self.folder / "outside"
        outside.mkdir()
        try:
            (self.folder / WORK_DIRECTORY).symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable")
        with self.assertRaises(CoverSaveError):
            save_cover(self.source, self.frame)
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_originals()

    def test_two_concurrent_saves_reserve_distinct_complete_pairs(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: save_cover(self.source, self.frame), range(2)))
        self.assertEqual({result.image_path.stem for result in results}, {"Holiday v1", "Holiday v2"})
        for result in results:
            self.assertEqual(result.video_path.stem, result.image_path.stem)
            self.assertEqual(result.video_path.read_bytes(), self.original_video)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_rollback_preserves_output_modified_by_another_program(self):
        publish = cover_save._publish_no_replace
        count = 0

        def fail_after_other_edit(staged, target):
            nonlocal count
            count += 1
            if count == 2:
                (self.folder / "Holiday v1.JPEG").write_bytes(b"new data from another program")
                raise OSError("video output failed")
            publish(staged, target)

        with patch.object(cover_save, "_publish_no_replace", side_effect=fail_after_other_edit):
            with self.assertRaisesRegex(CoverSaveError, "已保留"):
                save_cover(self.source, self.frame)
        self.assertEqual((self.folder / "Holiday v1.JPEG").read_bytes(), b"new data from another program")
        self.assert_originals()
        self.assert_no_pending_files()

    def test_atomic_replace_failure_leaves_original_and_no_backup(self):
        with patch.object(cover_save.os, "replace", side_effect=PermissionError("file in use")):
            with self.assertRaises(PermissionError):
                save_cover(self.source, self.frame, "overwrite")
        self.assert_originals()
        self.assert_no_pending_files()

    def test_real_portrait_video_frame_heic_copy_and_overwrite_stay_true_heic(self):
        from livegallery.frame_reader import FrameReader
        from livegallery.frame_tools import resolve_frame_tools
        from livegallery.heic_encoder import _executable
        from livegallery.imaging import load_image
        try:
            _executable()
            resolve_frame_tools()
        except OSError:
            self.skipTest("native encoder/frame tools unavailable in this developer environment")
        fixtures = Path(__file__).parent / "fixtures"
        heic = self.folder / "Portrait.HEIC"
        movie = self.folder / "Portrait.MOV"
        heic.write_bytes((fixtures / "Live_H264.heic").read_bytes())
        movie.write_bytes((fixtures / "Portrait_HEVC.MOV").read_bytes())
        original_heic, original_movie = heic.read_bytes(), movie.read_bytes()
        reader = FrameReader(movie)
        try:
            selected = reader.export(9)
        finally:
            reader.close()
        self.assertLess(selected.width, selected.height)
        source = CoverSource.capture(MediaItem("portrait", heic, movie))
        copied = save_cover(source, selected, "copy")
        self.assertEqual(copied.image_path.name, "Portrait v1.HEIC")
        self.assertEqual(copied.video_path.name, "Portrait v1.MOV")
        self.assertEqual(heic.read_bytes(), original_heic)
        self.assertEqual(movie.read_bytes(), original_movie)
        self.assertEqual(copied.video_path.read_bytes(), original_movie)
        overwritten = save_cover(source, selected, "overwrite")
        self.assertEqual(overwritten.image_path, heic)
        self.assertIsNone(overwritten.backup_path)
        self.assertEqual(movie.read_bytes(), original_movie)
        self.assertNotEqual(heic.read_bytes(), original_heic)
        for path in (copied.image_path, heic):
            header = path.read_bytes()[:128]
            self.assertEqual(header[4:8], b"ftyp")
            self.assertFalse(header.startswith(b"\xff\xd8"))
            decoded = load_image(path, None)
            self.assertEqual(decoded.size, selected.size)
            for x, y in ((.1, .1), (.9, .1), (.1, .9), (.9, .9)):
                position = (int(selected.width * x), int(selected.height * y))
                expected, actual = selected.getpixel(position), decoded.getpixel(position)
                self.assertLess(max(abs(a - b) for a, b in zip(expected, actual)), 35)
        self.assert_no_pending_files()
        self.assertFalse((self.folder / ".LiveViewer-backups").exists())


if __name__ == "__main__":
    unittest.main()
