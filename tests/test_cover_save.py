import errno
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from livegallery import cover_save
from livegallery.cover_save import CoverSaveError, CoverSource, SourceChangedError, WORK_DIRECTORY, save_cover
from livegallery.models import MediaItem
from livegallery.livephoto_metadata import photo_identifier, validate_pair
from tests.movie_helpers import assert_video_media_preserved


class CoverSaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.photo = self.folder / "Holiday.JPEG"
        self.video = self.folder / "Holiday.MOV"
        Image.new("RGB", (80, 60), "blue").save(self.photo, "JPEG")
        self.video.write_bytes((Path(__file__).parent / "fixtures" / "JPEG_PAIR.mov").read_bytes())
        self.original_image = self.photo.read_bytes()
        self.original_video = self.video.read_bytes()
        self.item = MediaItem("photo", self.photo, self.video)
        self.source = CoverSource.capture(self.item)
        self.frame = Image.new("RGB", (120, 90), "red")

    def tearDown(self):
        self.photo.chmod(0o600)
        self.video.chmod(0o600)
        self.folder.chmod(0o700)
        self.directory.cleanup()

    def assert_originals(self):
        self.assertEqual(self.photo.read_bytes(), self.original_image)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def assert_no_pending_files(self):
        work = self.folder / WORK_DIRECTORY
        self.assertTrue(not work.exists() or not list(work.iterdir()))

    def test_windows_directory_readonly_marker_is_not_an_acl_denial(self):
        directory = SimpleNamespace(stat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR | 0o555))
        regular = SimpleNamespace(stat=lambda: SimpleNamespace(st_mode=stat.S_IFREG | 0o444))
        with patch.object(cover_save.os, 'name', 'nt'):
            cover_save._writable(directory)
            with self.assertRaises(CoverSaveError):
                cover_save._writable(regular)
        with patch.object(cover_save.os, 'name', 'posix'):
            with self.assertRaises(CoverSaveError):
                cover_save._writable(directory)
            with self.assertRaises(CoverSaveError):
                cover_save._writable(regular)

    def test_copy_pair_shares_version_extension_and_live_photo_metadata(self):
        result = save_cover(self.source, self.frame)
        self.assertEqual(result.image_path.name, "Holiday v1.JPEG")
        self.assertEqual(result.video_path.name, "Holiday v1.MOV")
        self.assertTrue(result.created_copy)
        self.assertIsNone(result.backup_path)
        with Image.open(result.image_path) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, self.frame.size)
        assert_video_media_preserved(self, self.original_video, result.video_path.read_bytes())
        validate_pair(result.image_path, result.video_path, photo_identifier(result.image_path), 0)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_overwrite_replaces_validated_pair_and_keeps_no_backup(self):
        original_replace = cover_save.os.replace
        observations = []

        def replace(staged, target):
            observations.append((Path(target), self.photo.read_bytes(), Path(staged).read_bytes()[:3]))
            return original_replace(staged, target)

        with patch.object(cover_save.os, "replace", side_effect=replace):
            result = save_cover(self.source, self.frame, "overwrite")
        self.assertEqual([row[0] for row in observations], [self.photo, self.video])
        self.assertEqual(observations[0][1:], (self.original_image, b"\xff\xd8\xff"))
        self.assertEqual(result.image_path, self.photo)
        self.assertFalse(result.created_copy)
        self.assertIsNone(result.backup_path)
        assert_video_media_preserved(self, self.original_video, self.video.read_bytes())
        validate_pair(self.photo, self.video, photo_identifier(self.photo), 0)
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
        heic.write_bytes((Path(__file__).parent / "fixtures" / "Live_H264.heic").read_bytes())
        original_heic = heic.read_bytes()
        source = CoverSource.capture(MediaItem("heic", heic, self.video))

        def fake_heic(image, target, **kwargs):
            image.save(target, "JPEG")

        for mode in ("copy", "overwrite"):
            with self.assertRaises(CoverSaveError):
                save_cover(source, self.frame, mode, heic_encoder=fake_heic)
            self.assertEqual(heic.read_bytes(), original_heic)
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
            assert_video_media_preserved(self, self.original_video, result.video_path.read_bytes())
            validate_pair(result.image_path, result.video_path, photo_identifier(result.image_path), 0)
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

    def test_selected_time_and_fresh_identifier_are_written_into_both_outputs(self):
        first = save_cover(self.source, self.frame, frame_time_seconds=.4, frame_duration_seconds=.08)
        first_id = photo_identifier(first.image_path)
        validate_pair(first.image_path, first.video_path, first_id, .4)
        second = save_cover(self.source, self.frame, 'overwrite',
                            frame_time_seconds=.8, frame_duration_seconds=.04)
        second_id = photo_identifier(second.image_path)
        self.assertNotEqual(first_id, second_id)
        validate_pair(second.image_path, second.video_path, second_id, .8)
        assert_video_media_preserved(self, self.original_video, self.video.read_bytes())

    def test_pair_validation_failure_happens_before_any_publication(self):
        with patch.object(cover_save, 'validate_pair', side_effect=ValueError('invalid pairing')), \
                patch.object(cover_save.os, 'replace') as replace, \
                patch.object(cover_save, '_publish_no_replace') as publish:
            for mode in ('copy', 'overwrite'):
                with self.assertRaisesRegex(ValueError, 'invalid pairing'):
                    save_cover(self.source, self.frame, mode)
            replace.assert_not_called()
            publish.assert_not_called()
        self.assert_originals()
        self.assert_no_pending_files()

    def test_overwrite_second_replace_failure_restores_original_pair(self):
        replace = cover_save.os.replace
        def fail_movie(staged, target):
            if Path(target) == self.video:
                raise PermissionError('MOV still in use')
            replace(staged, target)
        with patch.object(cover_save.os, 'replace', side_effect=fail_movie):
            with self.assertRaisesRegex(PermissionError, 'MOV still in use'):
                save_cover(self.source, self.frame, 'overwrite')
        self.assert_originals()
        self.assert_no_pending_files()

    def test_overwrite_cancellation_between_replacements_restores_original_pair(self):
        replace, cancel = cover_save.os.replace, threading.Event()
        def cancel_after_first(staged, target):
            replace(staged, target)
            if Path(staged).name.startswith('cover'):
                cancel.set()
        with patch.object(cover_save.os, 'replace', side_effect=cancel_after_first):
            with self.assertRaises(InterruptedError):
                save_cover(self.source, self.frame, 'overwrite', cancel_event=cancel)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_interrupt_immediately_after_os_replace_restores_both_originals(self):
        replace = cover_save.os.replace
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                source = CoverSource.capture(self.item)
                def interrupt_after_replace(staged, target):
                    replace(staged, target)
                    if Path(staged).name.startswith('cover'):
                        raise interruption('interrupted between replacement and tracking')
                with patch.object(cover_save.os, 'replace', side_effect=interrupt_after_replace):
                    with self.assertRaises(interruption):
                        save_cover(source, self.frame, 'overwrite')
                self.assert_originals()
                self.assert_no_pending_files()

    def test_failed_rollback_retains_verified_originals_and_recovery_record(self):
        replace = cover_save.os.replace
        def fail_commit_and_restore(staged, target):
            if Path(target) == self.video or Path(staged).name.startswith('original-'):
                raise PermissionError('simulated locked destination')
            replace(staged, target)
        with patch.object(cover_save.os, 'replace', side_effect=fail_commit_and_restore):
            with self.assertRaisesRegex(CoverSaveError, '恢复副本已保留'):
                save_cover(self.source, self.frame, 'overwrite')
        recovery = list((self.folder / WORK_DIRECTORY).glob('*/recovery.json'))
        self.assertEqual(len(recovery), 1)
        transaction = recovery[0].parent
        self.assertEqual((transaction / 'original-image.JPEG').read_bytes(), self.original_image)
        self.assertEqual((transaction / 'original-video.MOV').read_bytes(), self.original_video)
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def test_overwrite_rollback_does_not_replace_concurrently_modified_output(self):
        replace = cover_save.os.replace
        def external_edit_then_fail(staged, target):
            if Path(target) == self.video:
                self.photo.write_bytes(b'external editor changed image')
                raise PermissionError('video unavailable')
            replace(staged, target)
        with patch.object(cover_save.os, 'replace', side_effect=external_edit_then_fail):
            with self.assertRaisesRegex(CoverSaveError, '恢复副本已保留'):
                save_cover(self.source, self.frame, 'overwrite')
        self.assertEqual(self.photo.read_bytes(), b'external editor changed image')
        self.assertEqual(self.video.read_bytes(), self.original_video)
        self.assertEqual(len(list((self.folder / WORK_DIRECTORY).glob('*/original-image.JPEG'))), 1)

    def test_read_only_video_is_not_replaced(self):
        self.video.chmod(0o444)
        with self.assertRaises(CoverSaveError):
            save_cover(self.source, self.frame, 'overwrite')
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
        assert_video_media_preserved(self, original_movie, copied.video_path.read_bytes())
        overwritten = save_cover(source, selected, "overwrite")
        self.assertEqual(overwritten.image_path, heic)
        self.assertIsNone(overwritten.backup_path)
        assert_video_media_preserved(self, original_movie, movie.read_bytes())
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
