"""Synthetic-only regression tests of the accepted native metadata writer."""
import copy
from fractions import Fraction
import hashlib
import json
import subprocess
from pathlib import Path
import struct
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from PIL import Image, ExifTags
from PIL.TiffImagePlugin import IFDRational

ROOT = Path(__file__).resolve().parents[1]
from livegallery import live_photo_native_image as writer
from livegallery.heic_encoder import encode_heic
from livegallery.live_photo_movie import inspect_live_photo_movie, rewrite_live_photo_movie

RUN = None
OLD = '11111111-2222-3333-4444-555555555555'
NEW = '66666666-7777-8888-9999-AAAAAAAAAAAA'
OPAQUE = b'opaque-synthetic-payload\0\x81\xff'
LONG8 = 8595185700
DATE, ZONE = '2025:02:03 04:05:06', '+08:00'
EVIDENCE = {'schema': 'live-photo-method-lab.exiftool-writer-tests.v1', 'private_media_used': False,
            'gui_started': False, 'iphone_import_verified': False, 'artifacts': str(RUN), 'images': [], 'movies': []}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_note(identifier, *, unaligned=False):
    if len(identifier) != 36 or not identifier.isascii():
        raise ValueError('Synthetic fixture identifier must be 36 ASCII characters')
    values = [(1, 9, 1, struct.pack('>i', 14)),
              (8, 10, 3, struct.pack('>6i', 1, 3, -2, 5, 7, 11)),
              (17, 2, 37, identifier.encode('ascii') + b'\0'),
              (23, 16, 1, struct.pack('>Q', LONG8)), (65433, 7, len(OPAQUE), OPAQUE)]
    data = bytearray()
    entries = []
    base = 14 + 2 + 12 * len(values) + 4
    for tag, kind, amount, value in values:
        if len(value) <= 4:
            location = value.ljust(4, b'\0')
        else:
            if not unaligned and (base + len(data)) % 2:
                data += b'\0'
            location = struct.pack('>I', base + len(data))
            data += value
        entries.append(struct.pack('>HHI', tag, kind, amount) + location)
    return b'Apple iOS\0\0\1MM' + struct.pack('>H', len(values)) + b''.join(entries) + b'\0' * 4 + data


def synthetic_note(unaligned=False):
    return make_note(OLD, unaligned=unaligned)


def exif(note):
    value = Image.Exif()
    value[0x10f], value[0x110] = 'Apple', 'Synthetic test source'
    value[0x112] = 1
    value[0x8769] = {0x927c: note, 0x9003: DATE, 0x9011: ZONE, 0xA002: 96, 0xA003: 64}
    value[0x8825] = {0: b'\2\3\0\0', 1: 'N', 2: (25.0, 0.0, 0.0), 3: 'E', 4: (121.0, 0.0, 0.0)}
    return value


def complex_exif(*, with_gps_version=False, endian='>'):
    value = exif(synthetic_note())
    value.endian = endian
    value[0x131] = 'MUST NOT COPY source software'
    value[0x100], value[0x101] = 96, 64
    value[0x8769].update({0x9004: '2025:02:03 04:05:07', 0x9011: '+05:45',
        0x9012: '-03:30', 0x9291: '000012300', 0x9292: '987654321', 0x829a: IFDRational(1, 137)})
    value[0x8825] = {1: 'S', 2: (IFDRational(25), IFDRational(17), IFDRational(199999997, 10000000)),
        3: 'W', 4: (IFDRational(121), IFDRational(44), IFDRational(298765432, 10000000)),
        5: 1, 6: IFDRational(123456789, 1000000),
        7: (IFDRational(13), IFDRational(7), IFDRational(123456789, 10000000)),
        8: '07', 9: 'A', 10: '3', 11: IFDRational(7, 13),
        12: 'K', 13: IFDRational(77, 19), 14: 'T', 15: IFDRational(11, 37),
        16: 'M', 17: IFDRational(279234567, 1000000), 18: 'WGS-84',
        23: 'T', 24: IFDRational(292765431, 1000000),
        27: b'ASCII\0\0\0synthetic processing', 29: '2025:02:02', 30: 1, 31: IFDRational(31, 17)}
    if with_gps_version:
        value[0x8825][0] = b'\2\3\0\0'
    return value


def raw_exif(path):
    with Image.open(path) as image:
        raw = image.info.get('exif', b'')
        if image.format == 'HEIF':
            from pi_heif import open_heif
            raw = open_heif(path).info['exif']
    result = Image.Exif()
    result.load(raw)
    return result


def exact_values(value):
    if isinstance(value, IFDRational):
        return ('rational', value.numerator, value.denominator)
    if isinstance(value, (tuple, list)):
        return tuple(exact_values(item) for item in value)
    if isinstance(value, dict):
        return {key: exact_values(item) for key, item in value.items()}
    return value


def tiff_entry(data, directory, tag):
    endian = '<' if data[:2] == b'II' else '>'
    count = struct.unpack_from(endian + 'H', data, directory)[0]
    for offset in range(directory + 2, directory + 2 + count * 12, 12):
        if struct.unpack_from(endian + 'H', data, offset)[0] == tag:
            return offset
    raise AssertionError(f'missing synthetic tag {tag:x}')


class EngineImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global RUN
        cls._temporary = tempfile.TemporaryDirectory(prefix='liveviewer-native-image-')
        cls.addClassCleanup(cls._temporary.cleanup)
        RUN = Path(cls._temporary.name)
        cls.originals = {}
        cls.image = Image.new('RGB', (96, 64), '#aa3388')
        cls.pixels = Image.new('RGB', (80, 120), '#226688')
        cls.sources = {}
        for kind in ('JPEG', 'HEIC'):
            path = RUN / ('original' + ('.JPG' if kind == 'JPEG' else '.HEIC'))
            if kind == 'JPEG':
                cls.image.save(path, 'JPEG', quality=100, exif=exif(synthetic_note()).tobytes())
            else:
                encode_heic(cls.image, path, exif=exif(synthetic_note()).tobytes())
            cls.sources[kind] = path
            cls.originals[str(path)] = digest(path)
        cls.plain_movie = ROOT / 'tests/fixtures/Live_HEVC.MOV'
        cls.movie = RUN / 'original.MOV'
        rewrite_live_photo_movie(cls.plain_movie, cls.movie, OLD, Fraction(0))
        cls.originals[str(cls.movie)] = digest(cls.movie)

    @classmethod
    def tearDownClass(cls):
        if {path: digest(path) for path in cls.originals} != cls.originals:
            raise AssertionError('Synthetic input unexpectedly changed')
        EVIDENCE['source_sha256'] = cls.originals
        EVIDENCE['sources_unchanged'] = True

    def output(self, suffix):
        return RUN / (self.id().split('.')[-1] + '-' + uuid.uuid4().hex[:8] + suffix)

    def test_jpeg_and_heic_sources_to_both_formats_keep_every_nonid_makernote_field(self):
        for source_kind, source in self.sources.items():
            for output_kind in ('JPEG', 'HEIC'):
                with self.subTest(source=source_kind, output=output_kind):
                    output = self.output('.JPG' if output_kind == 'JPEG' else '.HEIC')
                    result = writer.write_cover(source, self.pixels, output, NEW, output_kind)
                    self.assertTrue(result['typed_all_nonid_values_equal'])
                    self.assertTrue(result['capture_tags_preserved'])
                    self.assertTrue(result['pixels_after_metadata_write_identical'])
                    self.assertEqual(result['asset_identifier'], NEW)
                    raw, parsed = writer._read_note(output, None)
                    self.assertEqual(parsed[1][23], (16, 1, struct.pack('>Q', LONG8)))
                    self.assertEqual(parsed[1][65433], (7, len(OPAQUE), OPAQUE))
                    self.assertEqual(parsed[1][17], (2, 37, NEW.encode() + b'\0'))
                    self.assertEqual(result['output_sha256'], digest(output))
                    with Image.open(output) as decoded:
                        self.assertEqual(decoded.size, self.pixels.size)
                        self.assertEqual(decoded.format, 'JPEG' if output_kind == 'JPEG' else 'HEIF')
                    persisted = raw_exif(output)
                    self.assertEqual((persisted[0x100], persisted[0x101]), self.pixels.size)
                    details = persisted.get_ifd(0x8769)
                    self.assertEqual((details[0xA002], details[0xA003]), self.pixels.size)
                    properties = json.loads(writer._run(['-j', '-n', '-Make', '-Model', '-DateTimeOriginal',
                        '-OffsetTimeOriginal', '-GPSLatitude', '-GPSLongitude', '-Orientation', '-ModifyDate', output]))[0]
                    self.assertEqual(properties['DateTimeOriginal'], DATE)
                    self.assertEqual(properties['OffsetTimeOriginal'], ZONE)
                    self.assertEqual(properties['GPSLatitude'], 25)
                    self.assertEqual(properties['GPSLongitude'], 121)
                    self.assertEqual(properties['Orientation'], 1)
                    self.assertIn('ModifyDate', properties)
                    EVIDENCE['images'].append({'source_format': source_kind, 'output_format': output_kind,
                                               'path': str(output), 'verification': result, 'properties': properties})

    def test_alignment_rewrite_reports_physical_difference_but_preserves_typed_values(self):
        source, target = self.output('.JPG'), self.output('.JPG')
        self.image.save(source, 'JPEG', exif=exif(synthetic_note(unaligned=True)))
        before = digest(source)
        result = writer.write_cover(source, self.pixels, target, NEW, 'JPEG')
        self.assertTrue(result['typed_all_nonid_values_equal'])
        self.assertFalse(result['physical_equal_except_identifier'])
        self.assertEqual(digest(source), before)

    def test_precise_capture_without_gps_version_preserves_all_rationals_and_optional_fields(self):
        for source_kind in ('JPEG', 'HEIC'):
            for output_kind in ('JPEG', 'HEIC'):
                with self.subTest(source=source_kind, output=output_kind):
                    source = RUN / ('precise-source-' + uuid.uuid4().hex[:8] + ('.JPG' if source_kind == 'JPEG' else '.HEIC'))
                    raw = complex_exif().tobytes()
                    if source_kind == 'JPEG':
                        self.image.save(source, 'JPEG', exif=raw)
                    else:
                        encode_heic(self.image, source, exif=raw)
                    before, original = digest(source), raw_exif(source)
                    target = RUN / ('precise-target-' + uuid.uuid4().hex[:8] + ('.JPG' if output_kind == 'JPEG' else '.HEIC'))
                    result = writer.write_cover(source, self.pixels, target, NEW, output_kind)
                    final = raw_exif(target)
                    self.assertEqual(exact_values(final.get_ifd(0x8825)), exact_values(original.get_ifd(0x8825)))
                    self.assertNotIn(0, final.get_ifd(0x8825))
                    for key in (0x9003, 0x9004, 0x9011, 0x9012, 0x9291, 0x9292):
                        self.assertEqual(final.get_ifd(0x8769)[key], original.get_ifd(0x8769)[key])
                    self.assertEqual((final[0x10f], final[0x110]), (original[0x10f], original[0x110]))
                    self.assertEqual(final[0x112], 1)
                    self.assertEqual((final.get_ifd(0x8769)[0xa002], final.get_ifd(0x8769)[0xa003]), self.pixels.size)
                    self.assertNotIn(0x131, final)
                    self.assertEqual((final[0x100], final[0x101]), self.pixels.size)
                    self.assertNotEqual((final[0x100], final[0x101]), (original[0x100], original[0x101]))
                    self.assertNotIn(0x829a, final.get_ifd(0x8769))
                    self.assertFalse(final.get_ifd(ExifTags.IFD.IFD1))
                    self.assertTrue(result['capture_tags_preserved'])
                    self.assertTrue(result['typed_all_nonid_values_equal'])
                    self.assertEqual(digest(source), before)

    def test_little_endian_capture_and_absent_optional_groups(self):
        for present in (False, True):
            with self.subTest(gps_present=present):
                value = complex_exif(with_gps_version=True, endian='<')
                if not present:
                    del value[0x8825]
                    for tag in (0x9004, 0x9012, 0x9291, 0x9292):
                        del value[0x8769][tag]
                source = RUN / ('little-source-' + str(present) + '.jpg')
                target = RUN / ('little-target-' + str(present) + '.jpg')
                self.image.save(source, 'JPEG', exif=value.tobytes())
                before = writer._capture_raw(source, None)
                self.assertEqual(before['endian'], '<')
                result = writer.write_cover(source, self.pixels, target, NEW, 'JPEG')
                self.assertEqual(writer._capture_raw(target, None), before)
                self.assertTrue(result['capture_typed_raw_fields_preserved'])

    def test_raw_capture_parser_rejects_ambiguous_and_damaged_directories(self):
        original = complex_exif().tobytes()[6:]
        root = struct.unpack_from('>I', original, 4)[0]
        gps_pointer = tiff_entry(original, root, 0x8825)
        gps = struct.unpack_from('>I', original, gps_pointer + 8)[0]
        altitude = tiff_entry(original, gps, 6)
        count = struct.unpack_from('>H', original, gps)[0]
        variants = {}
        data = bytearray(original)
        struct.pack_into('>I', data, gps_pointer + 8, root)
        variants['cycle'] = bytes(data)
        data = bytearray(original)
        data[gps + 2 + (count - 1) * 12:gps + 4 + (count - 1) * 12] = data[gps + 2:gps + 4]
        variants['duplicate'] = bytes(data)
        data = bytearray(original)
        struct.pack_into('>I', data, altitude + 8, len(data) + 64)
        variants['outside'] = bytes(data)
        data = bytearray(original)
        location = struct.unpack_from('>I', data, altitude + 8)[0]
        struct.pack_into('>I', data, location + 4, 0)
        variants['zero-denominator'] = bytes(data)
        data = bytearray(original)
        struct.pack_into('>H', data, altitude + 2, 13)
        variants['gps-pointer'] = bytes(data)
        data = bytearray(original)
        struct.pack_into('>H', data, 2, 43)
        variants['bigtiff'] = bytes(data)
        for label, content in variants.items():
            with self.subTest(case=label):
                path = RUN / ('damaged-' + label + '.jpg')
                self.image.save(path, 'JPEG', exif=b'Exif\0\0' + content)
                with self.assertRaises(ValueError):
                    writer._capture_raw(path, None)

    def test_capture_changes_report_only_field_names_and_reject_any_loss(self):
        source = RUN / 'exact-change-source.jpg'
        self.image.save(source, 'JPEG', exif=complex_exif().tobytes())
        original = writer._capture_raw(source, None)
        variants = []
        changed = copy.deepcopy(original)
        kind, count, value = changed['GPS'][6]
        changed['GPS'][6] = (kind, count, value[:-1] + bytes([value[-1] ^ 1]))
        variants.append((changed, 'GPSAltitude'))
        missing = copy.deepcopy(original)
        del missing['GPS'][17]
        variants.append((missing, 'GPSImgDirection'))
        added = copy.deepcopy(original)
        added['GPS'][0] = (1, 4, b'\2\3\0\0')
        variants.append((added, 'GPSVersionID'))
        for altered, name in variants:
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, name) as failure:
                writer._verify_capture_raw(original, altered)
            self.assertNotIn('123456789', str(failure.exception))
            self.assertNotIn('279234567', str(failure.exception))

    def test_raw_capture_rejects_partially_overlapping_directory_tables(self):
        data = b'MM' + struct.pack('>HIH', 42, 8, 1)
        data += struct.pack('>HHII', 0x8769, 4, 1, 22) + b'\0' * 6
        with patch.object(writer, '_read_exif', return_value=data):
            with self.assertRaisesRegex(ValueError, '重叠'):
                writer._capture_raw(RUN / 'not-read.jpg', None)

    def test_raw_capture_and_builder_reject_excessive_shared_payload_budget(self):
        count, amount, gps = 4096, 65536, 26
        payload_start = gps + 2 + count * 12 + 4
        data = b'MM' + struct.pack('>HIH', 42, 8, 1)
        data += struct.pack('>HHII', 0x8825, 4, 1, gps) + b'\0' * 4
        data += struct.pack('>H', count)
        data += b''.join(struct.pack('>HHII', 0x1000 + index, 7, amount, payload_start) for index in range(count))
        data += b'\0' * 4 + b'x' * amount
        with patch.object(writer, '_read_exif', return_value=data):
            with self.assertRaisesRegex(ValueError, '累计'):
                writer._capture_raw(RUN / 'not-read.jpg', None)
        capture = {'endian': '>', 'IFD0': {}, 'ExifIFD': {}, 'GPS': {0xff00: (7, amount, b'x' * amount)}}
        with self.assertRaisesRegex(ValueError, '范围'):
            writer._capture_block(capture, synthetic_note())

    def test_new_cover_does_not_copy_original_thumbnail_or_exposure(self):
        raw = bytearray(complex_exif().tobytes()[6:])
        root = struct.unpack_from('>I', raw, 4)[0]
        root_count = struct.unpack_from('>H', raw, root)[0]
        ifd1 = len(raw)
        struct.pack_into('>I', raw, root + 2 + root_count * 12, ifd1)
        thumbnail = b'\xff\xd8synthetic-old-thumbnail\xff\xd9'
        thumbnail_offset = ifd1 + 2 + 3 * 12 + 4
        raw += struct.pack('>H', 3)
        raw += struct.pack('>HHI', 0x103, 3, 1) + struct.pack('>H', 6) + b'\0\0'
        raw += struct.pack('>HHII', 0x201, 4, 1, thumbnail_offset)
        raw += struct.pack('>HHII', 0x202, 4, 1, len(thumbnail))
        raw += b'\0' * 4 + thumbnail
        source, output = RUN / 'thumbnail-source.jpg', RUN / 'thumbnail-target.jpg'
        self.image.save(source, 'JPEG', exif=b'Exif\0\0' + raw)
        self.assertTrue(raw_exif(source).get_ifd(ExifTags.IFD.IFD1))
        result = writer.write_cover(source, self.pixels, output, NEW, 'JPEG')
        final = raw_exif(output)
        self.assertFalse(final.get_ifd(ExifTags.IFD.IFD1))
        self.assertNotIn(b'synthetic-old-thumbnail', output.read_bytes())
        self.assertNotIn(0x829a, final.get_ifd(0x8769))
        self.assertTrue(result['capture_typed_raw_fields_preserved'])

    def test_long8_is_external_eight_bytes_not_inline_one_byte(self):
        raw = synthetic_note()
        _, fields, locations = writer._note(raw)
        self.assertEqual(fields[23], (16, 1, struct.pack('>Q', LONG8)))
        self.assertEqual(locations[23][1], 8)
        self.assertGreaterEqual(locations[23][0], 14 + 2 + 4 * 12 + 4)

    def test_changed_nonid_field_is_rejected(self):
        original = synthetic_note()
        altered = bytearray(original)
        start, size = writer._note(original)[2][23]
        altered[start + size - 1] ^= 1
        with self.assertRaisesRegex(ValueError, '非配对标识'):
            writer._verify_note((original, writer._note(original)), (bytes(altered), writer._note(altered)), NEW)

    def test_invalid_or_missing_makernotes_never_publishes(self):
        for data in (b'', b'Apple iOS\0\0\1MM' + b'\xff' * 12):
            with self.subTest(data=data):
                source, target = self.output('.JPG'), self.output('.JPG')
                self.image.save(source, 'JPEG', exif=exif(data))
                with self.assertRaises((ValueError, OSError)):
                    writer.write_cover(source, self.pixels, target, NEW, 'JPEG')
                self.assertFalse(target.exists())

    def test_existing_destination_and_invalid_uuid_preserve_every_file(self):
        target = self.output('.JPG')
        target.write_bytes(b'preserve-existing')
        with self.assertRaises(FileExistsError):
            writer.write_cover(self.sources['JPEG'], self.pixels, target, NEW, 'JPEG')
        self.assertEqual(target.read_bytes(), b'preserve-existing')
        with self.assertRaises(ValueError):
            writer.write_cover(self.sources['JPEG'], self.pixels, self.output('.JPG'), 'not-uuid', 'JPEG')

    def test_pre_cancel_and_copy_id_failure_leave_no_destination(self):
        target = self.output('.JPG')
        event = threading.Event(); event.set()
        with self.assertRaises(InterruptedError):
            writer.write_cover(self.sources['JPEG'], self.pixels, target, NEW, 'JPEG', event)
        self.assertFalse(target.exists())
        actual = writer._run
        def fail_edit(arguments, cancel_event=None):
            if any(str(arg).startswith('-Apple:ContentIdentifier=') for arg in arguments):
                raise OSError('synthetic second-step failure')
            return actual(arguments, cancel_event)
        with patch.object(writer, '_run', side_effect=fail_edit):
            with self.assertRaisesRegex(OSError, 'second-step'):
                writer.write_cover(self.sources['JPEG'], self.pixels, target, NEW, 'JPEG')
        self.assertFalse(target.exists())
        self.assertFalse(list(RUN.glob('._lpimage-*')))

    def test_copy_movie_changes_only_identifier_and_preserves_marker_tracks_and_samples(self):
        output = self.output('.MOV')
        result = writer.copy_movie_with_identifier(self.movie, output, NEW)
        before, after = inspect_live_photo_movie(self.movie), inspect_live_photo_movie(output)
        self.assertEqual(after.asset_identifier, NEW)
        self.assertEqual((before.photo_time, before.marker_count, before.track_count),
                         (after.photo_time, after.marker_count, after.track_count))
        self.assertTrue(result['all_track_samples_and_times_preserved'])
        self.assertEqual(result['output_sha256'], digest(output))
        EVIDENCE['movies'].append({'path': str(output), 'verification': result})

    def test_native_av_verifier_accepts_only_relocated_chunk_addresses(self):
        output = self.output('.MOV')
        writer.copy_movie_with_identifier(self.movie, output, NEW)
        before, after = writer.movie._load(self.movie), writer.movie._load(output)
        original_tracks = [track for track in before.tracks if track.handler in (b'vide', b'soun')]
        output_tracks = [track for track in after.tracks if track.handler in (b'vide', b'soun')]
        self.assertTrue(any(a.samples != b.samples for a, b in zip(original_tracks, output_tracks)))
        verified = writer.verify_native_movie_payload_preserved(self.movie, output)
        self.assertTrue(verified['all_encoded_samples_and_timing_preserved'])
        self.assertTrue(verified['all_track_structure_except_chunk_offsets_preserved'])

    def test_native_av_verifier_rejects_rotation_or_other_nonoffset_attribute_change(self):
        output = self.output('.MOV')
        writer.copy_movie_with_identifier(self.movie, output, NEW)
        loaded = writer.movie._load(output)
        track = next(track for track in loaded.tracks if track.handler == b'vide')
        tkhd = writer.movie._one(writer.movie._children(track.box), b'tkhd')
        matrix_at = 52 if tkhd.payload[0] else 40
        offset = loaded.moov.start + loaded.moov.header + track.box.start + track.box.header + tkhd.start + tkhd.header + matrix_at
        with output.open('r+b') as stream:
            stream.seek(offset)
            original = stream.read(4)
            stream.seek(offset)
            stream.write((int.from_bytes(original, 'big') ^ 0x10000).to_bytes(4, 'big'))
        with self.assertRaisesRegex(ValueError, '结构'):
            writer.verify_native_movie_payload_preserved(self.movie, output)

    def test_native_av_verifier_rejects_encoded_sample_change(self):
        output = self.output('.MOV')
        writer.copy_movie_with_identifier(self.movie, output, NEW)
        loaded = writer.movie._load(output)
        offset, size = next(track for track in loaded.tracks if track.handler == b'vide').samples[0]
        self.assertGreater(size, 0)
        with output.open('r+b') as stream:
            stream.seek(offset)
            original = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([original[0] ^ 1]))
        with self.assertRaisesRegex(ValueError, '编码样本'):
            writer.verify_native_movie_payload_preserved(self.movie, output)

    def test_movie_copy_preserves_five_track_mixed_metadata_and_auxv(self):
        from tests.test_live_photo_native_movie import native_source
        for auxiliary_video in (False, True):
            with self.subTest(auxv=auxiliary_video):
                source, output = self.output('.MOV'), self.output('.MOV')
                native_source(source)
                if auxiliary_video:
                    source.write_bytes(source.read_bytes().replace(b'soun', b'auxv', 1))
                before_bytes = source.read_bytes()
                before = writer.movie._load(source)
                self.assertEqual(len(before.tracks), 5)
                self.assertTrue(any(track.marker_ids and len(track.keys) > 1 for track in before.tracks))
                result = writer.copy_movie_with_identifier(source, output, NEW)
                after = writer.movie._load(output)
                self.assertEqual(source.read_bytes(), before_bytes)
                self.assertEqual(len(after.tracks), 5)
                self.assertEqual([track.handler for track in after.tracks], [track.handler for track in before.tracks])
                self.assertEqual(inspect_live_photo_movie(output).asset_identifier, NEW)
                self.assertTrue(result['all_track_samples_and_times_preserved'])
                self.assertTrue(result['track_properties_except_chunk_offsets_preserved'])
                EVIDENCE['movies'].append({'synthetic_five_tracks': True, 'auxv': auxiliary_video,
                                           'path': str(output), 'verification': result})

    def test_heic_and_original_marker_stage_share_new_id_and_preserve_auxiliary_data(self):
        from tests.test_live_photo_native_movie import native_source
        from livegallery.live_photo_native_movie import rewrite_original_marker

        source, staged, cover = self.output('.MOV'), self.output('.MOV'), self.output('.HEIC')
        native_source(source)
        source.write_bytes(source.read_bytes().replace(b'soun', b'auxv', 1))
        source_bytes = source.read_bytes()
        marker = rewrite_original_marker(source, staged, Fraction(1, 2))
        before_id_change = writer.movie._load(staged)
        before_samples = writer._sample_digests(before_id_change, None)
        image_report = writer.write_cover(self.sources['HEIC'], self.pixels, cover, NEW, 'HEIC')
        movie_report = writer.set_movie_identifier(staged, NEW)
        final = writer.movie._load(staged)
        info = inspect_live_photo_movie(staged)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(info.asset_identifier, NEW)
        self.assertEqual(info.photo_time, Fraction(1, 2))
        self.assertEqual(info.marker_count, 1)
        self.assertEqual(before_samples, writer._sample_digests(final, None))
        self.assertIn(b'auxv', [track.handler for track in final.tracks])
        self.assertTrue(marker['native_handler_flags_tref_preserved'])
        self.assertTrue(movie_report['track_properties_except_chunk_offsets_preserved'])
        self.assertTrue(image_report['capture_typed_raw_fields_preserved'])
        self.assertTrue(image_report['typed_all_nonid_values_equal'])
        self.assertEqual(writer._read_note(cover, None)[1][1][17], (2, 37, NEW.encode() + b'\0'))
        self.assertEqual(movie_report['output_sha256'], digest(staged))
        self.assertEqual(image_report['output_sha256'], digest(cover))

    def test_set_identifier_on_new_stage_preserves_stage_on_tool_failure(self):
        stage = self.output('.MOV')
        stage.write_bytes(self.movie.read_bytes())
        before = digest(stage)
        with patch.object(writer, '_run', side_effect=OSError('synthetic tool failure')):
            with self.assertRaisesRegex(OSError, 'tool failure'):
                writer.set_movie_identifier(stage, NEW)
        self.assertEqual(digest(stage), before)
        result = writer.set_movie_identifier(stage, NEW)
        self.assertEqual(inspect_live_photo_movie(stage).asset_identifier, NEW)
        self.assertEqual(result['output_sha256'], digest(stage))

    def test_set_identifier_can_create_key_on_new_unpaired_movie_without_timed_changes(self):
        stage = self.output('.MOV')
        stage.write_bytes(self.plain_movie.read_bytes())
        before = inspect_live_photo_movie(stage)
        result = writer.set_movie_identifier(stage, NEW)
        after = inspect_live_photo_movie(stage)
        self.assertEqual(after.asset_identifier, NEW)
        self.assertEqual((after.marker_count, after.photo_time, after.track_count),
                         (before.marker_count, before.photo_time, before.track_count))
        self.assertTrue(result['all_track_samples_and_times_preserved'])

    def test_unicode_space_and_emoji_paths_cover_jpeg_heic_and_movie_id_writes(self):
        folder = RUN / '中文 空格 📷'
        source_folder, output_folder = folder / '来源 🧪', folder / '未完成 输出 🎬'
        source_folder.mkdir(parents=True)
        output_folder.mkdir()
        before = {}
        for source_kind, original in self.sources.items():
            source = source_folder / ('源图片 ' + source_kind + ' 😺' + original.suffix)
            source.write_bytes(original.read_bytes())
            before[source] = digest(source)
            for output_kind in ('JPEG', 'HEIC'):
                output = output_folder / (source_kind + ' 新封面 🎉' + ('.JPG' if output_kind == 'JPEG' else '.HEIC'))
                result = writer.write_cover(source, self.pixels, output, NEW, output_kind)
                self.assertTrue(result['typed_all_nonid_values_equal'])
                self.assertTrue(result['capture_tags_preserved'])
                self.assertEqual(writer._read_note(output, None)[1][1][17], (2, 37, NEW.encode() + b'\0'))
                self.assertEqual(result['output_sha256'], digest(output))
        source_movie = source_folder / '原视频 空格 🧪.MOV'
        source_movie.write_bytes(self.movie.read_bytes())
        before[source_movie] = digest(source_movie)
        output_movie = output_folder / '新副本 🎞.MOV'
        result = writer.copy_movie_with_identifier(source_movie, output_movie, NEW)
        self.assertTrue(result['all_track_samples_and_times_preserved'])
        self.assertEqual(inspect_live_photo_movie(output_movie).asset_identifier, NEW)
        stage = output_folder / '新暂存 🔒.MOV'
        stage.write_bytes(output_movie.read_bytes())
        second_id = 'BBBBBBBB-CCCC-DDDD-EEEE-FFFFFFFFFFFF'
        result = writer.set_movie_identifier(stage, second_id)
        self.assertEqual(inspect_live_photo_movie(stage).asset_identifier, second_id)
        self.assertTrue(result['all_track_samples_and_times_preserved'])
        self.assertEqual({path: digest(path) for path in before}, before)
        EVIDENCE['unicode_paths'] = {'source_directory': str(source_folder),
            'output_directory': str(output_folder), 'image_cases': 4, 'movie_cases': 2, 'sources_unchanged': True}

    def test_exiftool_argfile_rejects_line_and_nul_injection_before_process(self):
        for value in ('a\n-DeleteOriginal', 'a\r-o', 'a\0b'):
            with self.subTest(value=value), patch.object(writer.subprocess, 'Popen') as create:
                with self.assertRaises(ValueError):
                    writer._run(['-j', value])
                create.assert_not_called()

    def test_exiftool_argfile_sends_utf8_only_once_across_communicate_timeouts(self):
        class Process:
            returncode = 0
            def __init__(self):
                self.calls = []
            def communicate(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    raise subprocess.TimeoutExpired('synthetic', .1)
                return b'{}', b''
            def poll(self):
                return 0
        process = Process()
        arguments = ['-j', 'C:/中文 空格/emoji 📷.jpg']
        with patch.object(writer.subprocess, 'Popen', return_value=process) as create:
            self.assertEqual(writer._run(arguments), b'{}')
        self.assertEqual(create.call_args.args[0][1:], ['-charset', 'filename=UTF8', '-@', '-'])
        self.assertEqual(create.call_args.kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(process.calls[0]['input'], ('\n'.join(arguments) + '\n').encode('utf8'))
        self.assertIsNone(process.calls[1]['input'])



class ExifToolLocationTests(unittest.TestCase):
    def test_source_uses_only_the_repository_vendor_directory(self):
        with patch.object(writer.sys, 'frozen', False, create=True), \
                patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(writer._tool(), ROOT / 'vendor/exiftool/exiftool.exe')

    def test_frozen_uses_only_the_bundle_tools_directory(self):
        bundle = ROOT / 'synthetic-frozen-root'
        with patch.object(writer.sys, 'frozen', True, create=True), \
                patch.object(writer.sys, '_MEIPASS', str(bundle), create=True), \
                patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(writer._tool(), bundle / 'tools/exiftool/exiftool.exe')

    def test_missing_bundled_tool_fails_without_searching_path(self):
        with patch.object(Path, 'is_file', return_value=False), \
                patch.object(writer.shutil, 'which') as search:
            with self.assertRaisesRegex(OSError, 'ExifTool'):
                writer._tool()
            search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
