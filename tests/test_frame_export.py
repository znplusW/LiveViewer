"""Exact full-size exports and transactional failure/cancellation protection."""
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageColor

from livegallery.cover_save import CoverSaveError, FileFingerprint, SourceChangedError, WORK_DIRECTORY
from livegallery.frame_export import FrameExportError, export_frame
from livegallery.frame_reader import FrameReader
from livegallery.heic_encoder import _executable
from livegallery.imaging import load_image


FIXTURES = Path(__file__).resolve().parent / 'fixtures'
VFR_COLORS = ('#d43d35', '#42bd51', '#3568d4', '#e2d63c', '#cf42c2', '#3fc2d1', '#df8d32')


class SmallReader:
    def __init__(self, path, cancel_event=None):
        self.path, self.cancel_event = path, cancel_event
        self.closed = False

    def export(self, index):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InterruptedError('cancelled')
        if index != 4:
            raise IndexError(index)
        return Image.new('RGB', (321, 243), '#426ca2')

    def close(self):
        self.closed = True


class FrameExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.video = self.base / 'Original.MOV'
        shutil.copyfile(FIXTURES / 'VFR_PAIR.MOV', self.video)
        self.original_video = self.video.read_bytes()
        self.source = FileFingerprint.capture(self.video)

    def assert_clean(self):
        root = self.base / WORK_DIRECTORY
        self.assertFalse(root.exists() and any(root.iterdir()))
        self.assertEqual(self.video.read_bytes(), self.original_video)

    def test_real_variable_rate_indices_export_exact_full_resolution_frames(self):
        with FrameReader(self.video) as reader:
            expected_size = reader.metadata.width, reader.metadata.height
        for index, color in enumerate(VFR_COLORS):
            target = self.base / f'frame {index}.png'
            result = export_frame(self.video, index, target, expected_video=self.source)
            self.assertEqual(result, target)
            with Image.open(result) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, expected_size)
                self.assertEqual(image.mode, 'RGB')
                actual = image.getpixel((image.width // 2, image.height // 2))
            expected = ImageColor.getrgb(color)
            self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 8)
        self.assertEqual(list(self.base.glob('*.MOV')), [self.video])
        self.assert_clean()

    def test_png_jpg_jpeg_are_real_formats_and_overwrite_only_after_validation(self):
        for suffix, expected_format in (('.PNG', 'PNG'), ('.JPG', 'JPEG'), ('.JPEG', 'JPEG')):
            with self.subTest(suffix=suffix):
                target = self.base / ('导出 当前帧' + suffix)
                target.write_bytes(b'previous destination bytes')
                export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
                with Image.open(target) as image:
                    image.load()
                    self.assertEqual(image.format, expected_format)
                    self.assertEqual(image.size, (321, 243))
                    self.assertEqual(image.mode, 'RGB')
        self.assert_clean()

    def test_true_heic_preserves_full_size_and_uses_existing_encoder(self):
        try:
            _executable()
        except OSError as error:
            self.skipTest(str(error))
        target = self.base / 'frame.HEIC'
        export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
        header = target.read_bytes()[:128]
        self.assertEqual(header[4:8], b'ftyp')
        self.assertIn(b'heic', header)
        image = load_image(target, None)
        self.assertEqual(image.size, (321, 243))
        expected = ImageColor.getrgb('#426ca2')
        self.assertLess(max(abs(a - b) for a, b in zip(image.getpixel((100, 100)), expected)), 8)
        self.assert_clean()

    def test_bad_encoder_outputs_and_wrong_dimensions_never_replace_target(self):
        target = self.base / 'safe.png'
        original = b'original target survives every failed stage'
        for kind in ('truncated', 'wrong-format', 'wrong-size', 'wrong-mode'):
            target.write_bytes(original)
            def malformed(image, stage, *args):
                if kind == 'truncated':
                    stage.write_bytes(b'\x89PNG\r\n\x1a\ninvalid')
                elif kind == 'wrong-format':
                    image.save(stage, 'JPEG')
                elif kind == 'wrong-size':
                    image.resize((20, 20)).save(stage, 'PNG')
                else:
                    image.convert('P').save(stage, 'PNG')
            with self.subTest(kind=kind), patch('livegallery.frame_export._encode_export', side_effect=malformed):
                with self.assertRaises((OSError, ValueError, FrameExportError)):
                    export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
            self.assertEqual(target.read_bytes(), original)
            self.assert_clean()

    def test_mislabeled_jpeg_from_heic_encoder_is_rejected(self):
        target = self.base / 'safe.heic'
        target.write_bytes(b'original target')
        def fake_heic(image, stage, **kwargs):
            image.save(stage, 'JPEG')
        with self.assertRaises(CoverSaveError):
            export_frame(self.video, 4, target, expected_video=self.source,
                         reader_factory=SmallReader, heic_encoder=fake_heic)
        self.assertEqual(target.read_bytes(), b'original target')
        self.assert_clean()

    def test_source_replaced_since_preview_or_during_export_is_rejected(self):
        target = self.base / 'safe.png'
        target.write_bytes(b'previous target')
        before = self.video.stat()
        changed = bytearray(self.original_video)
        changed[-1] ^= 1
        self.video.write_bytes(changed)
        os.utime(self.video, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(SourceChangedError):
            export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
        self.assertEqual(target.read_bytes(), b'previous target')
        self.video.write_bytes(self.original_video)
        self.source = FileFingerprint.capture(self.video)
        from livegallery.frame_export import _encode_export
        def source_changes(image, stage, *args):
            _encode_export(image, stage, *args)
            self.video.write_bytes(changed)
        with patch('livegallery.frame_export._encode_export', side_effect=source_changes):
            with self.assertRaises(SourceChangedError):
                export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
        self.assertEqual(target.read_bytes(), b'previous target')
        self.video.write_bytes(self.original_video)
        self.assert_clean()

    def test_cancellation_before_decode_and_after_encode_preserves_target(self):
        target = self.base / 'safe.png'
        target.write_bytes(b'previous target')
        cancel = threading.Event()
        cancel.set()
        factory = Mock(side_effect=AssertionError('must not start decoding'))
        with self.assertRaises(InterruptedError):
            export_frame(self.video, 4, target, expected_video=self.source,
                         cancel_event=cancel, reader_factory=factory)
        cancel.clear()
        from livegallery.frame_export import _encode_export
        def after_encode(image, stage, *args):
            _encode_export(image, stage, *args)
            cancel.set()
        with patch('livegallery.frame_export._encode_export', side_effect=after_encode):
            with self.assertRaises(InterruptedError):
                export_frame(self.video, 4, target, expected_video=self.source,
                             cancel_event=cancel, reader_factory=SmallReader)
        self.assertEqual(target.read_bytes(), b'previous target')
        self.assert_clean()

    def test_callback_cancel_and_decode_failure_close_independent_reader(self):
        target = self.base / 'safe.png'
        target.write_bytes(b'previous target')
        with self.assertRaises(InterruptedError):
            export_frame(self.video, 4, target, cancel_event=lambda: True, reader_factory=SmallReader)
        instances = []
        class BrokenReader(SmallReader):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(self)
            def export(self, index):
                raise OSError('decode failed')
        with self.assertRaises(OSError):
            export_frame(self.video, 4, target, expected_video=self.source, reader_factory=BrokenReader)
        self.assertTrue(instances[0].closed)
        self.assertEqual(target.read_bytes(), b'previous target')
        self.assert_clean()

    def test_publication_failure_and_concurrent_target_preserve_existing_bytes(self):
        target = self.base / 'safe.png'
        target.write_bytes(b'previous target')
        with patch('livegallery.frame_export.os.replace', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):
                export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
        self.assertEqual(target.read_bytes(), b'previous target')
        target.unlink()
        from livegallery.frame_export import _encode_export
        def concurrent_target(image, stage, *args):
            _encode_export(image, stage, *args)
            target.write_bytes(b'concurrent creator')
        with patch('livegallery.frame_export._encode_export', side_effect=concurrent_target):
            with self.assertRaises(FileExistsError):
                export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
        self.assertEqual(target.read_bytes(), b'concurrent creator')
        self.assert_clean()

    def test_existing_destination_changed_while_encoding_is_not_overwritten(self):
        target = self.base / 'safe.png'
        target.write_bytes(b'previous target')
        from livegallery.frame_export import _encode_export
        def target_changes(image, stage, *args):
            _encode_export(image, stage, *args)
            target.write_bytes(b'new bytes from another editor')
        with patch('livegallery.frame_export._encode_export', side_effect=target_changes):
            with self.assertRaises(SourceChangedError):
                export_frame(self.video, 4, target, expected_video=self.source, reader_factory=SmallReader)
        self.assertEqual(target.read_bytes(), b'new bytes from another editor')
        self.assert_clean()

    def test_target_video_alias_invalid_format_and_invalid_index_are_rejected(self):
        alias = self.base / 'video alias.png'
        os.link(self.video, alias)
        with self.assertRaises(FrameExportError):
            export_frame(self.video, 4, alias, expected_video=self.source, reader_factory=SmallReader)
        for name, index in (('image.webp', 4), ('image.png', -1), ('image.png', True)):
            with self.subTest(name=name, index=index):
                with self.assertRaises((ValueError, FrameExportError)):
                    export_frame(self.video, index, self.base / name, reader_factory=SmallReader)
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
