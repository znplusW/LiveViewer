"""Real HEVC HEIC round trips and failure-safe subprocess output handling."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

from livegallery.heic_encoder import _executable, encode_heic
from livegallery.imaging import load_image


class HeicEncoderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name) / '封面 HEIC.HEIC'
        self.image = Image.new('RGB', (321, 243), '#426ca2')

    def test_cancel_before_export_leaves_no_file(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            encode_heic(self.image, self.target, cancel_event=event)
        self.assertFalse(self.target.exists())

    def test_missing_component_never_deletes_existing_path(self):
        self.target.write_bytes(b'original')
        with patch('livegallery.heic_encoder._executable', side_effect=OSError('missing')):
            with self.assertRaises(OSError):
                encode_heic(self.image, self.target)
        self.assertEqual(self.target.read_bytes(), b'original')

    def test_existing_destination_cannot_be_replaced_or_deleted(self):
        self.target.write_bytes(b'original')
        with patch('livegallery.heic_encoder._executable', return_value=Path('helper')):
            with self.assertRaises(FileExistsError):
                encode_heic(self.image, self.target)
        self.assertEqual(self.target.read_bytes(), b'original')

    def test_failed_launch_cleans_only_its_new_stage(self):
        with patch('livegallery.heic_encoder._executable', return_value=Path('missing-helper')):
            with self.assertRaises(OSError):
                encode_heic(self.image, self.target)
        self.assertFalse(self.target.exists())

    def test_real_heic_preserves_odd_dimensions_and_decodes(self):
        try:
            _executable()
        except OSError as error:
            self.skipTest(str(error))
        encode_heic(self.image, self.target)
        header = self.target.read_bytes()[:128]
        self.assertEqual(header[4:8], b'ftyp')
        self.assertIn(b'heic', header)
        decoded = load_image(self.target, None)
        self.assertEqual(decoded.size, self.image.size)
        error = ImageStat.Stat(ImageChops.difference(decoded.convert('RGB'), self.image)).mean
        self.assertLess(max(error), 8)


if __name__ == '__main__':
    unittest.main()
