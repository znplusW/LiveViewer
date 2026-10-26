import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from livegallery import cover_save
from livegallery.cover_save import CoverSaveError, CoverSource, SourceChangedError, WORK_DIRECTORY, save_cover
from livegallery.models import MediaItem
from tests.live_photo_fixtures import create_live_photo_pair, prepare_live_photo_pair, apple_exif


class CoverSaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.photo = self.folder / "Holiday.HEIC"
        self.video = self.folder / "Holiday.MOV"
        create_live_photo_pair(self.photo, self.video)
        self.original_image = self.photo.read_bytes()
        self.original_video = self.video.read_bytes()
        from livegallery.live_photo_movie import media_payload_digests
        self.original_payloads = media_payload_digests(self.video)
        self.item = MediaItem("photo", self.photo, self.video)
        self.source = CoverSource.capture(self.item)
        self.frame = Image.new("RGB", (120, 90), "red")

    def tearDown(self):
        if self.photo.exists():
            self.photo.chmod(0o600)
        self.folder.chmod(0o700)
        self.directory.cleanup()

    def assert_pair(self, image, movie, photo_time=Fraction(0)):
        from livegallery.live_photo_image import read_content_identifier
        from livegallery.live_photo_movie import inspect_live_photo_movie, media_payload_digests
        identifier = read_content_identifier(image)
        info = inspect_live_photo_movie(movie)
        self.assertTrue(identifier)
        self.assertTrue(info.supported, info.unsupported_reason)
        self.assertEqual(info.asset_identifier, identifier)
        self.assertEqual(info.photo_time, photo_time)
        self.assertEqual(info.marker_count, 1)
        self.assertEqual(media_payload_digests(movie), self.original_payloads)
        return identifier

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

    def test_copy_pair_shares_new_identifier_marker_version_and_preserves_media_payload(self):
        result = save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertEqual(result.image_path.name, "Holiday v1.HEIC")
        self.assertEqual(result.video_path.name, "Holiday v1.MOV")
        self.assertTrue(result.created_copy)
        self.assertIsNone(result.backup_path)
        with Image.open(result.image_path) as image:
            self.assertEqual(image.format, "HEIF")
            self.assertEqual(image.size, self.frame.size)
        self.assertNotEqual(result.video_path.read_bytes(), self.original_video)
        self.assert_pair(result.image_path, result.video_path)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_overwrite_commits_both_resources_and_keeps_no_backup(self):
        original_replace = cover_save.os.replace
        observations = []

        def replace(staged, target):
            if Path(target) in (self.photo, self.video):
                observations.append((Path(target), self.photo.read_bytes(), self.video.read_bytes()))
            return original_replace(staged, target)

        with patch.object(cover_save.os, "replace", side_effect=replace):
            result = save_cover(self.source, self.frame, "overwrite", photo_time=Fraction(0))
        self.assertEqual([row[0] for row in observations], [self.photo, self.video])
        self.assertEqual(observations[0][1:], (self.original_image, self.original_video))
        self.assertNotEqual(observations[1][1], self.original_image)
        self.assertEqual(observations[1][2], self.original_video)
        self.assertEqual(result.image_path, self.photo)
        self.assertFalse(result.created_copy)
        self.assertIsNone(result.backup_path)
        self.assertNotEqual(self.video.read_bytes(), self.original_video)
        self.assert_pair(self.photo, self.video)
        with Image.open(self.photo) as image:
            self.assertEqual(image.size, self.frame.size)
        self.assert_no_pending_files()
        self.assertFalse((self.folder / ".LiveViewer-backups").exists())

    def test_case_insensitive_collision_any_extension_and_literal_version_stem(self):
        (self.folder / "HOLIDAY V1.PNG").write_bytes(b"existing")
        result = save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertEqual(result.image_path.stem, "Holiday v2")
        second = CoverSource.capture(MediaItem("copy", result.image_path, result.video_path))
        nested = save_cover(second, self.frame, photo_time=Fraction(0))
        self.assertEqual(nested.image_path.stem, "Holiday v2 v1")
        self.assertEqual(nested.video_path.stem, nested.image_path.stem)
        self.assert_originals()

    def test_copy_preserves_each_original_stem_case_with_shared_version_number(self):
        lower_video = self.folder / "holiday.MOV"
        self.video.rename(lower_video)
        self.video = lower_video
        self.item = MediaItem("photo", self.photo, self.video)
        self.source = CoverSource.capture(self.item)
        result = save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertEqual(result.image_path.name, "Holiday v1.HEIC")
        self.assertEqual(result.video_path.name, "holiday v1.MOV")
        self.assertEqual(result.image_path.stem.casefold(), result.video_path.stem.casefold())
        self.assert_originals()

    def test_source_mutation_since_edit_open_is_rejected(self):
        self.video.write_bytes(self.original_video + b"changed")
        with self.assertRaises(SourceChangedError):
            save_cover(self.source, self.frame, "overwrite", photo_time=Fraction(0))
        self.assertEqual(self.photo.read_bytes(), self.original_image)
        self.assert_no_pending_files()

    def test_source_mutation_during_encode_is_rejected_before_commit(self):
        encode = cover_save._encode_image

        def mutate(*args, **kwargs):
            encode(*args, **kwargs)
            self.video.write_bytes(self.original_video + b"changed")

        with patch.object(cover_save, "_encode_image", side_effect=mutate):
            with self.assertRaises(SourceChangedError):
                save_cover(self.source, self.frame, "overwrite", photo_time=Fraction(0))
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
                save_cover(self.source, self.frame, photo_time=Fraction(0))
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
                save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertEqual((self.folder / "Holiday v1.HEIC").read_bytes(), b"created by another program")
        self.assertFalse((self.folder / "Holiday v1.MOV").exists())
        self.assert_originals()
        self.assert_no_pending_files()

    def test_concurrent_other_format_same_stem_rolls_back_owned_output(self):
        publish = cover_save._publish_no_replace

        def race(staged, target):
            publish(staged, target)
            (self.folder / "HOLIDAY V1.JPG").write_bytes(b"other creator")

        with patch.object(cover_save, "_publish_no_replace", side_effect=race):
            with self.assertRaises(FileExistsError):
                save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertFalse((self.folder / "Holiday v1.HEIC").exists())
        self.assertEqual((self.folder / "HOLIDAY V1.JPG").read_bytes(), b"other creator")
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
                save_cover(self.source, self.frame, cancel_event=cancel, photo_time=Fraction(0))
        self.assertFalse(any(self.folder.glob("Holiday v1.*")))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_disk_full_or_encoder_failure_cleans_staging(self):
        def fail(image, target, *_args, **_kwargs):
            Path(target).write_bytes(b"partial image")
            raise OSError(errno.ENOSPC, "disk full")

        with patch.object(cover_save, "_encode_image", side_effect=fail):
            with self.assertRaises(OSError):
                save_cover(self.source, self.frame, "overwrite", photo_time=Fraction(0))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_read_only_original_is_not_replaced(self):
        self.photo.chmod(0o444)
        with self.assertRaises(CoverSaveError):
            save_cover(self.source, self.frame, "overwrite", photo_time=Fraction(0))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_cancel_before_saving_does_not_create_outputs(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            save_cover(self.source, self.frame, cancel_event=cancel, photo_time=Fraction(0))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_heic_cannot_be_saved_as_disguised_jpeg(self):
        heic = self.folder / "Holiday.HEIC"
        # Use the complete synthetic Apple source, then inject a fake encoder.
        original_heic = heic.read_bytes()
        source = CoverSource.capture(MediaItem("heic", heic, self.video))

        def fake_heic(image, target, **kwargs):
            image.save(target, "JPEG")

        for mode in ("copy", "overwrite"):
            with self.assertRaises(CoverSaveError):
                save_cover(source, self.frame, mode, heic_encoder=fake_heic, photo_time=Fraction(0))
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
            save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_originals()

    def test_two_concurrent_saves_reserve_distinct_complete_pairs(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: save_cover(self.source, self.frame, photo_time=Fraction(0)), range(2)))
        self.assertEqual({result.image_path.stem for result in results}, {"Holiday v1", "Holiday v2"})
        for result in results:
            self.assertEqual(result.video_path.stem, result.image_path.stem)
            self.assertNotEqual(result.video_path.read_bytes(), self.original_video)
            self.assert_pair(result.image_path, result.video_path)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_rollback_preserves_output_modified_by_another_program(self):
        publish = cover_save._publish_no_replace
        count = 0

        def fail_after_other_edit(staged, target):
            nonlocal count
            count += 1
            if count == 2:
                (self.folder / "Holiday v1.HEIC").write_bytes(b"new data from another program")
                raise OSError("video output failed")
            publish(staged, target)

        with patch.object(cover_save, "_publish_no_replace", side_effect=fail_after_other_edit):
            with self.assertRaisesRegex(CoverSaveError, "已保留") as caught:
                save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertNotIn('已保留恢复资料：', str(caught.exception))
        self.assertIn('外部修改的文件', str(caught.exception))
        self.assertEqual((self.folder / "Holiday v1.HEIC").read_bytes(), b"new data from another program")
        self.assert_originals()
        self.assert_no_pending_files()

    def test_atomic_replace_failure_leaves_original_and_no_backup(self):
        with patch.object(cover_save.os, "replace", side_effect=PermissionError("file in use")):
            with self.assertRaises(PermissionError):
                save_cover(self.source, self.frame, "overwrite", photo_time=Fraction(0))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_save_requires_exact_frame_time_and_rejects_non_frame_time(self):
        for value in (None, .04, Fraction(-1), Fraction(1, 1000)):
            error = ValueError if value == Fraction(1, 1000) else CoverSaveError
            with self.subTest(value=value), self.assertRaises(error):
                save_cover(self.source, self.frame, 'overwrite', photo_time=value)
            self.assert_originals()
            self.assert_no_pending_files()

    def test_every_copy_and_overwrite_gets_new_shared_identifier(self):
        identifier = 'Existing Apple association 字符串'
        prepare_live_photo_pair(self.photo, self.video, identifier=identifier)
        original_pair = (self.photo.read_bytes(), self.video.read_bytes())
        source = CoverSource.capture(self.item)
        copied = save_cover(source, self.frame, photo_time=Fraction(1, 25))
        identifiers = {identifier, self.assert_pair(copied.image_path, copied.video_path, Fraction(1, 25))}
        self.assertEqual(len(identifiers), 2)
        self.assertEqual((self.photo.read_bytes(), self.video.read_bytes()), original_pair)
        for point in (Fraction(4, 25), Fraction(11, 25)):
            changed = save_cover(CoverSource.capture(self.item), self.frame, 'overwrite', photo_time=point)
            current = self.assert_pair(changed.image_path, changed.video_path, point)
            self.assertNotIn(current, identifiers)
            self.assertEqual(changed.asset_identifier, current)
            identifiers.add(current)
        self.assert_no_pending_files()

    def test_mismatched_or_missing_identifier_gets_new_shared_identifier(self):
        from livegallery.heic_encoder import encode_heic
        prepare_live_photo_pair(self.photo, self.video, identifier='other-movie')
        stage = self.folder / 'image-stage.HEIC'
        encode_heic(self.frame, stage, exif=apple_exif('image-only'))
        stage.replace(self.photo)
        changed = save_cover(CoverSource.capture(self.item), self.frame, 'overwrite', photo_time=Fraction(1, 5))
        self.assertNotIn(self.assert_pair(changed.image_path, changed.video_path, Fraction(1, 5)),
                         ('image-only', 'other-movie'))
        self.assert_no_pending_files()

    def _crash_after_first_publication(self, mode='overwrite', after=1):
        # A separate process exits without Python finally/except cleanup. Its
        # next opener must recover the journal, not rely on this test's stack.
        code = '''
import os, sys
from pathlib import Path
from fractions import Fraction
from PIL import Image
from livegallery import cover_save as c
from livegallery.models import MediaItem
from tests.live_photo_fixtures import create_live_photo_pair, prepare_live_photo_pair, apple_exif
image, video, mode, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
source = c.CoverSource.capture(MediaItem('crash', image, video))
replace_original, publish_original = c.os.replace, c._publish_no_replace
def observed(target):
    global count
    if Path(target).suffix.lower() in ('.jpg', '.jpeg', '.heic', '.mov') and Path(target).parent == image.parent:
        count -= 1
        if count == 0: os._exit(73)
def replace(staged, target):
    replace_original(staged, target)
    observed(target)
def publish(staged, target):
    publish_original(staged, target)
    observed(target)
c.os.replace, c._publish_no_replace = replace, publish
c.save_cover(source, Image.new('RGB',(120,90),'red'), mode, photo_time=Fraction(1,25))
'''
        completed = subprocess.run([sys.executable, '-c', code, str(self.photo), str(self.video), mode, str(after)],
                                   capture_output=True, timeout=30,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(completed.returncode, 73, completed.stderr.decode('utf8', errors='replace'))
        transactions = list((self.folder / WORK_DIRECTORY).glob('*/journal.json'))
        self.assertEqual(len(transactions), 1)
        return transactions[0]

    def _use_jpeg_source(self):
        old = self.photo
        self.photo = self.folder / 'Holiday.JPEG'
        Image.new('RGB', (80, 60), 'blue').save(self.photo, 'JPEG', exif=apple_exif())
        old.unlink()
        self.original_image = self.photo.read_bytes()
        self.item = MediaItem('photo', self.photo, self.video)
        self.source = CoverSource.capture(self.item)

    def _extra_jpeg(self, name='holiday.jpg'):
        path = self.folder / name
        Image.new('RGB', (30, 20), 'green').save(path, 'JPEG', exif=apple_exif())
        return path

    def test_jpeg_overwrite_publishes_heic_then_retires_all_same_stem_jpegs(self):
        self._use_jpeg_source()
        extra = self._extra_jpeg()
        unrelated = self.folder / 'Holiday.PNG'
        unrelated.write_bytes(b'other image format is not retired')
        result = save_cover(self.source, self.frame, 'overwrite', photo_time=Fraction(1, 25))
        self.assertEqual(result.image_path, self.folder / 'Holiday.HEIC')
        self.assertFalse(self.photo.exists())
        self.assertFalse(extra.exists())
        self.assertTrue(unrelated.exists())
        self.assert_pair(result.image_path, result.video_path, Fraction(1, 25))
        self.assertEqual(result.source_image_path, self.photo)
        self.assert_no_pending_files()

    def test_heic_overwrite_retires_same_stem_jpg_and_jpeg_only(self):
        extras = [self._extra_jpeg('Holiday.JPG'), self._extra_jpeg('holiday.jpeg')]
        other = self._extra_jpeg('Different.JPG')
        result = save_cover(self.source, self.frame, 'overwrite', photo_time=Fraction(0))
        self.assertEqual(result.image_path, self.photo)
        self.assertTrue(self.photo.exists())
        self.assertTrue(other.exists())
        self.assertTrue(all(not path.exists() for path in extras))
        self.assert_pair(result.image_path, result.video_path)
        self.assert_no_pending_files()

    def test_copy_from_jpeg_keeps_all_original_jpeg_candidates(self):
        self._use_jpeg_source()
        extra = self._extra_jpeg()
        before = extra.read_bytes()
        result = save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertEqual(result.image_path.name, 'Holiday v1.HEIC')
        self.assertEqual(extra.read_bytes(), before)
        self.assert_originals()
        self.assert_pair(result.image_path, result.video_path)
        self.assert_no_pending_files()

    def test_jpeg_overwrite_refuses_existing_heic_without_touching_any_resource(self):
        self._use_jpeg_source()
        collision = self.folder / 'HOLIDAY.heic'
        collision.write_bytes(b'preexisting HEIC owned by another program')
        with self.assertRaises(FileExistsError):
            save_cover(self.source, self.frame, 'overwrite', photo_time=Fraction(0))
        self.assertEqual(collision.read_bytes(), b'preexisting HEIC owned by another program')
        self.assert_originals()
        self.assert_no_pending_files()

    def test_cancel_after_each_jpeg_retirement_restores_all_originals(self):
        self._use_jpeg_source()
        extra = self._extra_jpeg()
        extra_bytes = extra.read_bytes()
        publish = cover_save._publish_no_replace
        for position in (1, 2):
            with self.subTest(position=position):
                cancel = threading.Event()
                count = 0
                def retire(staged, target):
                    nonlocal count
                    publish(staged, target)
                    if Path(target).name.startswith('retired-image-'):
                        count += 1
                        if count == position:
                            cancel.set()
                with patch.object(cover_save, '_publish_no_replace', side_effect=retire):
                    with self.assertRaises(InterruptedError):
                        save_cover(CoverSource.capture(self.item), self.frame, 'overwrite',
                                   photo_time=Fraction(0), cancel_event=cancel)
                self.assert_originals()
                self.assertEqual(extra.read_bytes(), extra_bytes)
                self.assertFalse((self.folder / 'Holiday.HEIC').exists())
                self.assert_no_pending_files()

    def test_failure_retiring_second_candidate_restores_first_and_movie(self):
        self._use_jpeg_source()
        extra = self._extra_jpeg()
        extra_bytes = extra.read_bytes()
        publish = cover_save._publish_no_replace
        def retire(staged, target):
            if Path(target).name == 'retired-image-1':
                raise PermissionError('candidate occupied')
            publish(staged, target)
        with patch.object(cover_save, '_publish_no_replace', side_effect=retire), self.assertRaises(PermissionError):
            save_cover(self.source, self.frame, 'overwrite', photo_time=Fraction(0))
        self.assert_originals()
        self.assertEqual(extra.read_bytes(), extra_bytes)
        self.assertFalse((self.folder / 'Holiday.HEIC').exists())
        self.assert_no_pending_files()

    def test_jpeg_conversion_crash_after_each_publication_recovers_original_pair(self):
        self._use_jpeg_source()
        for position in (1, 2):
            with self.subTest(position=position):
                self._crash_after_first_publication(after=position)
                self.assertTrue((self.folder / 'Holiday.HEIC').exists())
                cover_save.recover_cover_transactions(self.folder)
                self.assert_originals()
                self.assertFalse((self.folder / 'Holiday.HEIC').exists())
                self.assert_no_pending_files()

    def _crash_during_retirement(self, phase):
        code = '''
import os, sys
from pathlib import Path
from fractions import Fraction
from PIL import Image
from livegallery import cover_save as c
from livegallery.models import MediaItem
image, video, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
source = c.CoverSource.capture(MediaItem('crash', image, video))
original = c._publish_no_replace
def publish(staged, target):
    original(staged, target)
    if Path(target).name == phase: os._exit(74)
c._publish_no_replace = publish
unlink = Path.unlink
def remove(path, *args, **kwargs):
    unlink(path, *args, **kwargs)
    if phase == 'committed-cleanup' and path.name == 'original-image': os._exit(74)
Path.unlink = remove
c.save_cover(source, Image.new('RGB',(120,90),'red'), 'overwrite', photo_time=Fraction(1,25))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.photo), str(self.video), phase],
                                capture_output=True, timeout=45,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 74, result.stderr.decode('utf8', errors='replace'))
        journals = list((self.folder / WORK_DIRECTORY).glob('*/journal.json'))
        self.assertEqual(len(journals), 1)
        return journals[0]

    def test_crash_after_each_retirement_is_recoverable_without_deleting_external_files(self):
        self._use_jpeg_source()
        extra = self._extra_jpeg()
        extra_bytes = extra.read_bytes()
        for phase in ('retired-image-0', 'retired-image-1'):
            with self.subTest(phase=phase):
                self._crash_during_retirement(phase)
                cover_save.recover_cover_transactions(self.folder)
                self.assert_originals()
                self.assertEqual(extra.read_bytes(), extra_bytes)
                self.assertFalse((self.folder / 'Holiday.HEIC').exists())
                self.assert_no_pending_files()

    def test_committed_cleanup_crash_preserves_new_pair_and_finishes_retired_cleanup(self):
        self._use_jpeg_source()
        extra = self._extra_jpeg()
        journal = self._crash_during_retirement('committed-cleanup')
        self.assertEqual(json.loads(journal.read_text('utf8'))['state'], 'committed')
        self.assertFalse(self.photo.exists())
        self.assertFalse(extra.exists())
        new = self.folder / 'Holiday.HEIC'
        self.assert_pair(new, self.video, Fraction(1, 25))
        final_bytes = new.read_bytes(), self.video.read_bytes()
        cover_save.recover_cover_transactions(self.folder)
        self.assertEqual((new.read_bytes(), self.video.read_bytes()), final_bytes)
        self.assert_no_pending_files()

    def test_external_edit_of_additional_jpeg_during_encoding_is_preserved(self):
        extra = self._extra_jpeg()
        encode = cover_save._encode_image
        def changed(*args, **kwargs):
            encode(*args, **kwargs)
            extra.write_bytes(b'external edit of candidate')
        with patch.object(cover_save, '_encode_image', side_effect=changed), self.assertRaises(SourceChangedError):
            save_cover(self.source, self.frame, 'overwrite', photo_time=Fraction(0))
        self.assertEqual(extra.read_bytes(), b'external edit of candidate')
        self.assert_originals()
        self.assert_no_pending_files()

    def test_external_recreation_at_retired_jpeg_name_is_never_overwritten(self):
        self._use_jpeg_source()
        journal = self._crash_during_retirement('retired-image-0')
        self.photo.write_bytes(b'external new source path')
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(self.photo.read_bytes(), b'external new source path')
        self.assertEqual((journal.parent / 'retired-image-0').read_bytes(), self.original_image)
        self.assertEqual((journal.parent / 'original-image').read_bytes(), self.original_image)
        self.assertTrue(journal.exists())

    def test_retirement_journal_rejects_wrong_stem_extension_and_duplicate_names(self):
        self._use_jpeg_source()
        journal = self._crash_during_retirement('retired-image-0')
        valid = json.loads(journal.read_text('utf8'))
        for key, value in (('name', '../elsewhere.JPG'), ('name', 'Other.JPG'),
                           ('name', 'Holiday.MOV'), ('retired', 'original-video')):
            document = json.loads(json.dumps(valid))
            document['retired_images'][0][key] = value
            journal.write_text(json.dumps(document), encoding='utf8')
            with self.subTest(key=key, value=value), self.assertRaises(CoverSaveError):
                cover_save.recover_cover_transactions(self.folder)
            self.assertTrue((journal.parent / 'retired-image-0').exists())
        document = json.loads(json.dumps(valid))
        document['retired_images'].append(dict(document['retired_images'][0], retired='retired-image-1'))
        journal.write_text(json.dumps(document), encoding='utf8')
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        journal.write_text(json.dumps(valid), encoding='utf8')
        cover_save.recover_cover_transactions(self.folder)
        self.assert_originals()

    def test_version_one_journal_still_recovers_same_path_overwrite(self):
        journal = self._crash_after_first_publication()
        document = json.loads(journal.read_text('utf8'))
        document['schema'] = 'liveviewer.cover-pair-transaction.v1'
        document.pop('retired_images')
        document.pop('staging_records')
        for row in document['entries']:
            row.pop('source')
        journal.write_text(json.dumps(document), encoding='utf8')
        cover_save.recover_cover_transactions(self.folder)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_cleanup_rejects_case_renamed_modified_owned_backup(self):
        journal = self._crash_after_first_publication()
        backup = journal.parent / 'original-video'
        renamed = journal.parent / 'ORIGINAL-VIDEO'
        backup.rename(renamed)
        renamed.write_bytes(b'external content under case alias')
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(renamed.read_bytes(), b'external content under case alias')
        self.assertTrue(journal.exists())

    def test_staging_recovery_preserves_unproven_content_even_with_known_role_name(self):
        journal = self._crash_after_first_publication()
        document = json.loads(journal.read_text('utf8'))
        document['state'], document['entries'], document['staging_records'] = 'staging', [], {}
        journal.write_text(json.dumps(document), encoding='utf8')
        backup = journal.parent / 'original-video'
        before = backup.read_bytes()
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(backup.read_bytes(), before)
        self.assertTrue(journal.exists())

    def test_copy_recovery_preserves_untracked_backup_role_file(self):
        journal = self._crash_after_first_publication('copy')
        unexpected = journal.parent / 'original-image'
        unexpected.write_bytes(b'unrelated external recovery material')
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(unexpected.read_bytes(), b'unrelated external recovery material')
        self.assertTrue(journal.exists())
        self.assert_originals()

    def test_copy_journal_cannot_claim_overwrite_backup_role(self):
        journal = self._crash_after_first_publication('copy')
        extra = journal.parent / 'original-image'
        extra.write_bytes(b'external copy-only recovery material')
        document = json.loads(journal.read_text('utf8'))
        document['staging_records']['original-image'] = cover_save._record(cover_save.FileFingerprint.capture(extra))
        journal.write_text(json.dumps(document), encoding='utf8')
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(extra.read_bytes(), b'external copy-only recovery material')
        self.assertTrue(journal.exists())

    def test_case_renamed_journal_is_removed_only_after_every_owned_data_file(self):
        journal = self._crash_after_first_publication()
        renamed = journal.parent / 'JOURNAL.JSON'
        journal.rename(renamed)
        removed = []
        unlink = Path.unlink
        def observed(path, *args, **kwargs):
            if path.parent == journal.parent:
                removed.append(path.name)
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', observed):
            cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(removed[-1], 'JOURNAL.JSON')
        self.assert_originals()
        self.assert_no_pending_files()

    def test_existing_lowercase_heic_overwrite_keeps_exact_source_path(self):
        renamed = self.folder / 'Holiday.heic'
        self.photo.rename(renamed)
        self.photo = renamed
        self.item = MediaItem('lower', self.photo, self.video)
        result = save_cover(CoverSource.capture(self.item), self.frame, 'overwrite', photo_time=Fraction(0))
        self.assertEqual(str(result.image_path), str(renamed))
        self.assert_pair(result.image_path, result.video_path)
        self.assert_no_pending_files()

    def test_process_interruption_recovers_overwrite_before_new_capture(self):
        self._crash_after_first_publication()
        self.assertNotEqual(self.photo.read_bytes(), self.original_image)
        recovered = CoverSource.capture(self.item)
        self.assertEqual(recovered.image.sha256, self.source.image.sha256)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_process_interruption_removes_only_owned_copy(self):
        self._crash_after_first_publication('copy')
        self.assertTrue((self.folder / 'Holiday v1.HEIC').exists())
        cover_save.recover_cover_transactions(self.folder)
        self.assertFalse(any(self.folder.glob('Holiday v1.*')))
        self.assert_originals()
        self.assert_no_pending_files()

    def test_process_interruption_after_second_replace_still_restores_both(self):
        self._crash_after_first_publication(after=2)
        self.assertNotEqual(self.video.read_bytes(), self.original_video)
        cover_save.recover_cover_transactions(self.folder)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_committed_recovery_hash_does_not_discard_backup_after_external_edit(self):
        journal = self._crash_after_first_publication(after=2)
        document = json.loads(journal.read_text('utf8'))
        document['state'] = 'committed'
        journal.write_text(json.dumps(document), encoding='utf8')
        matches = cover_save._matches
        def changed_during_hash(path, record):
            result = matches(path, record)
            if Path(path) == self.video and result:
                self.photo.write_bytes(b'External edit during committed recovery')
            return result
        with patch.object(cover_save, '_matches', side_effect=changed_during_hash):
            with self.assertRaises(CoverSaveError):
                cover_save.recover_cover_transactions(self.folder)
        self.assertTrue(journal.exists())
        self.assertEqual((journal.parent / 'original-image').read_bytes(), self.original_image)
        self.assertEqual(self.photo.read_bytes(), b'External edit during committed recovery')

    def test_cleanup_refuses_modified_recovery_file_even_when_original_is_already_restored(self):
        journal = self._crash_after_first_publication()
        self.photo.unlink()
        (journal.parent / 'original-image').replace(self.photo)
        (journal.parent / 'original-video').write_bytes(b'External material in recovery directory')
        with self.assertRaises(CoverSaveError):
            cover_save.recover_cover_transactions(self.folder)
        self.assertTrue(journal.exists())
        self.assertEqual((journal.parent / 'original-video').read_bytes(), b'External material in recovery directory')
        self.assert_originals()

    def test_second_overwrite_failure_and_keyboard_interrupt_restore_pair(self):
        original = cover_save.os.replace
        for error in (PermissionError('MOV occupied'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                def replace(staged, target):
                    if Path(target) == self.video and Path(staged).name == 'video.MOV':
                        raise error
                    return original(staged, target)
                with patch.object(cover_save.os, 'replace', side_effect=replace), self.assertRaises(type(error)):
                    save_cover(CoverSource.capture(self.item), self.frame, 'overwrite', photo_time=Fraction(0))
                self.assert_originals()
                self.assert_no_pending_files()

    def test_failed_rollback_keeps_journal_and_can_be_retried(self):
        journal = self._crash_after_first_publication()
        with patch.object(cover_save.os, 'replace', side_effect=PermissionError('restore occupied')):
            with self.assertRaises(PermissionError):
                cover_save.recover_cover_transactions(self.folder)
        self.assertTrue(journal.exists())
        self.assertEqual((journal.parent / 'original-image').read_bytes(), self.original_image)
        cover_save.recover_cover_transactions(self.folder)
        self.assert_originals()
        self.assert_no_pending_files()

    def test_recovery_rechecks_target_after_backup_hash_and_preserves_external_edit(self):
        journal = self._crash_after_first_publication()
        matches = cover_save._matches
        def changed_during_hash(path, record):
            result = matches(path, record)
            if Path(path).name == 'original-image' and result:
                self.photo.write_bytes(b'External edit during long backup hash')
            return result
        with patch.object(cover_save, '_matches', side_effect=changed_during_hash):
            with self.assertRaises(CoverSaveError):
                cover_save.recover_cover_transactions(self.folder)
        self.assertEqual(self.photo.read_bytes(), b'External edit during long backup hash')
        self.assertTrue(journal.exists())
        self.assertEqual((journal.parent / 'original-image').read_bytes(), self.original_image)

    def test_final_pair_hash_cannot_report_success_after_external_image_edit(self):
        matches = cover_save._matches
        changed = False
        def changed_during_hash(path, record):
            nonlocal changed
            result = matches(path, record)
            if Path(path).name == 'Holiday v1.MOV' and result and not changed:
                changed = True
                (self.folder / 'Holiday v1.HEIC').write_bytes(b'External edit during final MOV hash')
            return result
        with patch.object(cover_save, '_matches', side_effect=changed_during_hash):
            with self.assertRaises(CoverSaveError):
                save_cover(self.source, self.frame, photo_time=Fraction(0))
        self.assertTrue(changed)
        self.assertEqual((self.folder / 'Holiday v1.HEIC').read_bytes(), b'External edit during final MOV hash')
        self.assertFalse((self.folder / 'Holiday v1.MOV').exists())
        self.assert_originals()

    def test_journal_rejects_windows_aliases_roles_and_reparse_without_touching_targets(self):
        journal = self._crash_after_first_publication()
        valid = journal.read_text('utf8')
        new_image = self.photo.read_bytes()
        for target in ('../outside.jpg', 'Holiday.JPEG:payload', 'Holiday.JPEG.', 'Holiday.JPEG ', 'CON.jpg'):
            document = json.loads(valid)
            document['entries'][0]['target'] = target
            journal.write_text(json.dumps(document), encoding='utf8')
            with self.subTest(target=target), self.assertRaises(CoverSaveError):
                cover_save.recover_cover_transactions(self.folder)
            self.assertEqual(self.photo.read_bytes(), new_image)
        for key, value in (('backup', 'original-video'), ('staged', 'video.MOV'), ('target', self.video.name)):
            document = json.loads(valid)
            document['entries'][0][key] = value
            journal.write_text(json.dumps(document), encoding='utf8')
            with self.subTest(key=key), self.assertRaises(CoverSaveError):
                cover_save.recover_cover_transactions(self.folder)
        journal.write_text(valid, encoding='utf8')
        real_reparse = cover_save._is_reparse
        for linked in (journal.parent, journal.parent / 'original-image'):
            with patch.object(cover_save, '_is_reparse', side_effect=lambda path: Path(path) == linked or real_reparse(path)):
                with self.assertRaises(CoverSaveError):
                    cover_save.recover_cover_transactions(self.folder)
            self.assertEqual(self.photo.read_bytes(), new_image)
        cover_save.recover_cover_transactions(self.folder)
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
        prepare_live_photo_pair(heic, movie)
        original_heic, original_movie = heic.read_bytes(), movie.read_bytes()
        reader = FrameReader(movie)
        try:
            selected = reader.export(9)
            selected_time = reader.metadata.frames[9].presentation_time
        finally:
            reader.close()
        self.assertLess(selected.width, selected.height)
        source = CoverSource.capture(MediaItem("portrait", heic, movie))
        copied = save_cover(source, selected, "copy", photo_time=selected_time)
        self.assertEqual(copied.image_path.name, "Portrait v1.HEIC")
        self.assertEqual(copied.video_path.name, "Portrait v1.MOV")
        self.assertEqual(heic.read_bytes(), original_heic)
        self.assertEqual(movie.read_bytes(), original_movie)
        from livegallery.live_photo_movie import media_payload_digests, inspect_live_photo_movie
        from livegallery.live_photo_native_image import verify_native_movie_payload_preserved
        from livegallery.live_photo_image import read_content_identifier
        self.assertEqual(media_payload_digests(movie), media_payload_digests(copied.video_path))
        verify_native_movie_payload_preserved(movie, copied.video_path)
        self.assertEqual(inspect_live_photo_movie(copied.video_path).photo_time, selected_time)
        self.assertEqual(inspect_live_photo_movie(copied.video_path).asset_identifier, read_content_identifier(copied.image_path))
        overwritten = save_cover(source, selected, "overwrite", photo_time=selected_time)
        self.assertEqual(overwritten.image_path, heic)
        self.assertIsNone(overwritten.backup_path)
        self.assertNotEqual(movie.read_bytes(), original_movie)
        self.assertEqual(inspect_live_photo_movie(movie).photo_time, selected_time)
        self.assertEqual(inspect_live_photo_movie(movie).asset_identifier, read_content_identifier(heic))
        self.assertEqual(media_payload_digests(copied.video_path), media_payload_digests(movie))
        verify_native_movie_payload_preserved(copied.video_path, movie)
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
