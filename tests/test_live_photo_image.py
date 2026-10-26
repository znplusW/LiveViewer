"""Synthetic real JPEG/HEIC cover metadata, with no user-media input."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, TiffImagePlugin

from livegallery import live_photo_image as metadata
from livegallery.heic_encoder import _executable, encode_heic


R = TiffImagePlugin.IFDRational
ID = 'Non-UUID:Keep-This-Case'


class LivePhotoImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'original.JPG'
        self.picture = Image.new('RGB', (83, 61), '#82a150')
        self.picture.save(self.source)

    def source_with_exif(self, details=None, primary=None, gps=None):
        exif = Image.Exif()
        for tag, value in (primary or {}).items():
            exif[tag] = value
        if details:
            exif[metadata.EXIF_IFD] = details
        if gps:
            exif[metadata.GPS_IFD] = gps
        self.picture.save(self.source, exif=exif.tobytes())

    def save_cover(self, identifier=ID, exif=None, name='cover.jpg'):
        destination = self.root / name
        encoded = metadata.build_cover_exif(self.source, identifier, (37, 29)) if exif is None else exif
        image = Image.new('RGB', (37, 29), '#4281a0')
        if destination.suffix == '.heic':
            encode_heic(image, destination, exif=encoded)
        else:
            image.save(destination, exif=encoded)
        return destination

    def test_real_jpeg_retains_capture_metadata_and_rebuilds_pixel_metadata(self):
        details = {0x9003: '2021:02:03 04:05:06', 0x9004: '2021:02:03 04:05:07',
                   0x9011: '+08:00', 0x9012: '-04:30', 0x9291: '123', 0x9292: '45',
                   0x9000: b'0232', 0xA002: 83, 0xA003: 61, 0x829A: R(1, 200),
                   0xA420: 'old-exif-image-unique-id', metadata.MAKER_NOTE: b'old opaque note'}
        gps = {0: b'\x02\x03\x00\x00', 1: 'N', 2: (R(31), R(12), R(17, 2)),
               3: 'E', 4: (R(121), R(28), R(7)), 5: b'\x00', 6: R(15, 2),
               7: (R(20), R(5), R(6)), 29: '2021:02:02', 18: 'WGS-84', 31: R(3, 2)}
        self.source_with_exif(details, {0x010F: 'Synthetic Camera', 0x0110: 'Fixture',
                                      0x0112: 6, 0x0100: 83, 0x0101: 61}, gps)
        before = self.source.read_bytes()
        target = self.save_cover()
        metadata.validate_cover_metadata(target, ID)
        self.assertEqual(metadata.read_content_identifier(target), ID)
        primary, copied, location, size = metadata._metadata(target)
        self.assertEqual(size, (37, 29))
        self.assertEqual(primary[0x010F], 'Synthetic Camera')
        for tag in (0x9003, 0x9004, 0x9011, 0x9012, 0x9291, 0x9292, 0x9000):
            self.assertEqual(copied[tag], details[tag])
        self.assertNotIn(0x829A, copied)
        self.assertNotIn(0xA420, copied)
        self.assertNotEqual(copied[metadata.MAKER_NOTE], details[metadata.MAKER_NOTE])
        for tag, value in gps.items():
            self.assertEqual(location[tag], value)
        self.assertEqual(self.source.read_bytes(), before)

    def test_modification_time_and_zone_are_new_but_capture_unknown_zone_stays_unknown(self):
        self.source_with_exif({0x9003: '2020:01:02 03:04:05', 0x9004: '2020:01:02 03:04:05',
                               0x9010: '-07:00', 0x9290: '999'}, {0x0132: '2000:01:01 00:00:00'})
        now = datetime(2026, 10, 25, 15, 30, 1, 123456, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(metadata, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            target = self.save_cover()
        primary, details, _, _ = metadata._metadata(target)
        # astimezone is the application's actual system zone, not a guessed capture zone.
        local = now.astimezone()
        self.assertEqual(primary[0x0132], local.strftime('%Y:%m:%d %H:%M:%S'))
        zone = local.strftime('%z')
        self.assertEqual(details[0x9010], zone[:3] + ':' + zone[3:])
        self.assertEqual(details[0x9290], '123456')
        self.assertEqual(details[0x9003], '2020:01:02 03:04:05')
        self.assertNotIn(0x9011, details)
        self.assertNotIn(0x9012, details)
        self.assertNotIn(0x9000, details)

    def test_missing_exif_does_not_invent_capture_dates_or_gps(self):
        target = self.save_cover()
        _, details, gps, _ = metadata._metadata(target)
        self.assertNotIn(0x9003, details)
        self.assertNotIn(0x9004, details)
        self.assertNotIn(0x9000, details)
        self.assertEqual(gps, {})
        metadata.validate_cover_metadata(target, ID)

    def test_invalid_capture_values_are_not_propagated(self):
        self.source_with_exif({0x9003: '2023:02:29 01:02:03', 0x9004: 'bad',
                               0x9011: '+14:30', 0x9012: 'UTC', 0x9291: 'bad'},
                              gps={1: 'N', 2: (R(91), R(0), R(0)),
                                   3: 'E', 4: (R(179), R(61), R(0)), 29: '2021:02:30'})
        _, details, gps, _ = metadata._metadata(self.save_cover())
        for tag in (0x9003, 0x9004, 0x9011, 0x9012, 0x9291):
            self.assertNotIn(tag, details)
        self.assertEqual(gps, {})

    def test_identifiers_preserve_short_non_uuid_and_unicode_values(self):
        for index, identifier in enumerate(('a', 'ab', 'abc', 'abcd', ID, '关联-ABC-é')):
            with self.subTest(identifier=identifier):
                target = self.save_cover(identifier, name=f'cover-{index}.jpg')
                self.assertEqual(metadata.read_content_identifier(target), identifier)
                metadata.validate_cover_metadata(target, identifier)

    def test_invalid_identifiers_fail_before_any_source_access(self):
        for identifier in ('', ' ', 'bad\x00id', 'bad\nid', 'bad\x7fid', 'a' * 513, '\ud800', None):
            with self.subTest(identifier=repr(identifier)), self.assertRaises(ValueError):
                metadata.build_cover_exif(self.root / 'missing.jpg', identifier, (10, 10))

    def test_bad_dimensions_fail_before_source_access(self):
        for size in ((0, 10), (10, -1), (True, 10), (1.5, 10), (10,), (10001, 10000)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                metadata.build_cover_exif(self.root / 'missing.jpg', ID, size)

    def test_exif_image_unique_id_is_not_apple_content_identifier(self):
        self.source_with_exif({0xA420: ID})
        self.assertIsNone(metadata.read_content_identifier(self.source))

    def test_malformed_notes_are_unusable_and_validation_rejects_them(self):
        good = metadata._maker_note(ID)
        malformed = [good[:15], good[:-1], good[:12] + b'XX' + good[14:],
                     good[:14] + b'\xff\xff' + good[16:],
                     good[:24] + struct.pack('>I', 0xfffffff0) + good[28:],
                     good[:18] + struct.pack('>H', 7) + good[20:]]
        for index, note in enumerate(malformed):
            with self.subTest(index=index):
                self.source_with_exif({metadata.MAKER_NOTE: note})
                self.assertIsNone(metadata.read_content_identifier(self.source))
                with self.assertRaises(ValueError):
                    metadata.validate_cover_metadata(self.source, ID)

    def test_duplicate_identifier_and_nonterminated_identifier_are_rejected(self):
        entry = struct.pack('>HHI', 17, 2, 2) + b'a\0\0\0'
        duplicate = metadata._APPLE_PREFIX + b'MM' + struct.pack('>H', 2) + entry * 2 + b'\0' * 4
        nonterminated = metadata._maker_note('abc')[:24] + b'abcd' + b'\0' * 4
        for note in (duplicate, nonterminated):
            with self.assertRaises(ValueError):
                metadata._read_maker_identifier(note)

    def test_little_endian_existing_apple_note_is_read_without_changing_id(self):
        data = ID.encode() + b'\0'
        note = (metadata._APPLE_PREFIX + b'II' + struct.pack('<H', 1)
                + struct.pack('<HHII', 17, 2, len(data), 32) + b'\0' * 4 + data)
        self.source_with_exif({metadata.MAKER_NOTE: note})
        self.assertEqual(metadata.read_content_identifier(self.source), ID)

    def test_persisted_wrong_identifier_orientation_and_dimensions_fail_validation(self):
        target = self.save_cover()
        with self.assertRaises(ValueError):
            metadata.validate_cover_metadata(target, 'different')
        for primary_tag, value in ((0x0112, 6), (0x0100, 83), (0x0101, 61)):
            exif = Image.Exif()
            exif.load(metadata.build_cover_exif(self.source, ID, (37, 29)))
            exif[primary_tag] = value
            with self.subTest(tag=primary_tag), self.assertRaises(ValueError):
                metadata.validate_cover_metadata(self.save_cover(exif=exif.tobytes()), ID)
        exif[0x0112], exif[0x0100], exif[0x0101] = 1, 37, 29
        details = dict(exif.get_ifd(metadata.EXIF_IFD))
        details[0xA002] = 999
        exif[metadata.EXIF_IFD] = details
        with self.assertRaises(ValueError):
            metadata.validate_cover_metadata(self.save_cover(exif=exif.tobytes()), ID)

    def test_actual_format_must_match_supported_extension(self):
        fake = self.root / 'wrong.heic'
        fake.write_bytes(self.source.read_bytes())
        with self.assertRaises(ValueError):
            metadata.read_content_identifier(fake)
        png = self.root / 'unsupported.png'
        self.picture.save(png)
        with self.assertRaises(ValueError):
            metadata.build_cover_exif(png, ID, (37, 29))

    def test_heic_argument_validation_preserves_destination_and_avoids_launch(self):
        target = self.root / 'existing.heic'
        target.write_bytes(b'keep')
        for exif in (b'', b'not exif', b'Exif\0\0BAD!' + b'\0' * 20,
                     b'Exif\0\0II*\0' + b'\0' * (60 * 1024), 'string'):
            with self.subTest(exif=str(exif)[:30]), patch('livegallery.heic_encoder.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    encode_heic(self.picture, target, exif=exif)
                launch.assert_not_called()
                self.assertEqual(target.read_bytes(), b'keep')

    def test_real_heic_roundtrip_and_heic_source_retain_apple_identifier_and_capture(self):
        self.source_with_exif({0x9003: '2022:04:05 06:07:08', 0x9011: '-03:00'})
        target = self.save_cover(name='cover.heic')
        metadata.validate_cover_metadata(target, ID)
        self.assertEqual(metadata.read_content_identifier(target), ID)
        with Image.open(target) as actual:
            actual.load()
            self.assertEqual(actual.size, (37, 29))
        second = self.root / 'from-heic.jpg'
        exif = metadata.build_cover_exif(target, 'new-shared-id', (41, 33))
        Image.new('RGB', (41, 33)).save(second, exif=exif)
        metadata.validate_cover_metadata(second, 'new-shared-id')
        _, details, _, _ = metadata._metadata(second)
        self.assertEqual(details[0x9003], '2022:04:05 06:07:08')
        self.assertEqual(details[0x9011], '-03:00')

    def test_heic_validation_uses_stored_orientation_not_pillow_normalization(self):
        exif = Image.Exif()
        exif.load(metadata.build_cover_exif(self.source, ID, (37, 29)))
        exif[0x0112] = 6
        target = self.save_cover(exif=exif.tobytes(), name='wrong-orientation.heic')
        with Image.open(target) as normalized:
            self.assertEqual(normalized.getexif().get(0x0112), 1)
        with self.assertRaises(ValueError):
            metadata.validate_cover_metadata(target, ID)

    def test_native_helper_rejects_truncated_and_oversized_metadata(self):
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        executable = _executable()
        exif = metadata.build_cover_exif(self.source, ID, self.picture.size)
        for count, data in ((len(exif), exif[:-1]), (61441, b''), (14, b'bad' * 5), ('14junk', b'')):
            with self.subTest(count=count):
                result = subprocess.run([str(executable), '83', '61', '95', str(count)],
                                        input=self.picture.tobytes() + data, capture_output=True,
                                        creationflags=flags, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
                self.assertIn(b'EXIF', result.stderr)


if __name__ == '__main__':
    unittest.main()
