import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from PIL import Image

from livegallery import livephoto_metadata as metadata
from livegallery.frame_reader import FrameReader
from livegallery.frame_tools import resolve_frame_tools

FIXTURES = Path(__file__).parent / 'fixtures'


def essence(path):
    """Independent ffprobe packet digests + timing, excluding metadata packets."""
    probe = resolve_frame_tools().ffprobe
    data = json.loads(subprocess.check_output([str(probe), '-v', 'error', '-show_packets',
        '-show_data_hash', 'sha256', '-show_entries',
        'packet=codec_type,pts,dts,duration,size,data_hash', '-of', 'json', str(path)]))
    return [packet for packet in data['packets'] if packet['codec_type'] in ('audio', 'video')]


class LivePhotoMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.movie = self.root / 'live.MOV'
        shutil.copyfile(FIXTURES / 'VFR_PAIR.MOV', self.movie)
        self.identifier = str(uuid.uuid4()).upper()
        self.image = self.root / 'live.jpeg'
        self.source = FIXTURES / 'VFR_PAIR.jpg'

    def tearDown(self):
        self.temp.cleanup()

    def encode(self, seconds=.44):
        with FrameReader(self.movie) as reader:
            index = min(range(reader.frame_count), key=lambda i: abs(reader.metadata.frames[i].pts_seconds - seconds))
            frame = reader.metadata.frames[index]
            image = reader.export(index)
        image.save(self.image, quality=95, exif=metadata.make_photo_exif(self.source, self.identifier, image.size))
        metadata.rewrite_movie(self.movie, self.identifier, frame.pts_seconds, frame.duration_seconds)
        return frame

    def test_vfr_pair_is_complete_and_video_packets_are_unchanged(self):
        before = essence(self.movie)
        frame = self.encode()
        self.assertEqual(frame.pts_seconds, .44)
        result = metadata.validate_pair(self.image, self.movie, self.identifier, .44)
        self.assertAlmostEqual(result['still_time_seconds'], .44, places=5)
        self.assertEqual(essence(self.movie), before)
        self.assertNotEqual(self.movie.read_bytes(), (FIXTURES / 'VFR_PAIR.MOV').read_bytes())

    def test_edited_pair_replaces_old_marker_instead_of_adding_duplicate(self):
        self.encode()
        first_id = self.identifier
        self.identifier = str(uuid.uuid4()).upper()
        self.encode(0)
        self.assertNotEqual(first_id, self.identifier)
        metadata.validate_pair(self.image, self.movie, self.identifier, 0)
        _, children = metadata._movie(self.movie)
        tracks = [payload for kind, payload in children if kind == b'trak' and metadata.STILL_TIME in payload]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(essence(self.movie), essence(FIXTURES / 'VFR_PAIR.MOV'))

    def test_portrait_hevc_retains_rotation_and_all_media_packets(self):
        shutil.copyfile(FIXTURES / 'Portrait_HEVC.MOV', self.movie)
        _, original = metadata._movie(self.movie)
        tracks = [payload for kind, payload in original if kind == b'trak']
        self.encode(.4)
        _, edited = metadata._movie(self.movie)
        edited_tracks = [payload for kind, payload in edited if kind == b'trak']
        self.assertTrue(all(track in edited_tracks for track in tracks))
        self.assertEqual(essence(self.movie), essence(FIXTURES / 'Portrait_HEVC.MOV'))

    def test_last_vfr_frame_has_a_marker_within_movie_duration(self):
        with FrameReader(self.movie) as reader:
            final = reader.metadata.frames[-1]
        self.encode(final.pts_seconds)
        metadata.validate_pair(self.image, self.movie, self.identifier, final.pts_seconds)

    def test_mismatched_image_identifier_or_marker_refuses_pair(self):
        self.encode()
        with self.assertRaises(metadata.LivePhotoMetadataError):
            metadata.validate_pair(self.image, self.movie, str(uuid.uuid4()), .44)
        with self.assertRaises(metadata.LivePhotoMetadataError):
            metadata.validate_pair(self.image, self.movie, self.identifier, .1)

    def test_maker_note_uses_undefined_type_required_by_apple_imageio(self):
        self.encode()
        with Image.open(self.image) as image:
            data = image.info['exif'][6:]
            sub = image.getexif()[34665]
        count = struct.unpack_from('>H', data, sub)[0]
        entries = [struct.unpack_from('>HHII', data, sub + 2 + 12 * i) for i in range(count)]
        self.assertEqual(next(entry for entry in entries if entry[0] == 37500)[1], 7)
        # Pillow's nested bytes default to BYTE; prove the invalid variant is
        # rejected even though Pillow itself can still extract the identifier.
        with Image.open(self.image) as image:
            exif = image.getexif()
            exif[34665] = exif.get_ifd(34665)
            pixels = image.copy()
        pixels.save(self.image, exif=exif)
        with self.assertRaises(metadata.LivePhotoMetadataError):
            metadata.photo_identifier(self.image)

    def test_preserves_capture_date_timezone_and_gps_but_not_old_sensor_makernote(self):
        source = self.root / 'source.jpg'
        exif = Image.Exif()
        exif[34665] = {36867: '2020:04:05 06:07:08', 36881: '+08:00', 37521: '123', 33434: .01}
        exif[34853] = {1: 'N', 2: (22.0, 3.0, 1.0), 3: 'E', 4: (113.0, 4.0, 2.0)}
        Image.new('RGB', (20, 30)).save(source, exif=exif)
        value = metadata.make_photo_exif(source, self.identifier, (30, 40))
        exif = Image.Exif(); exif.load(value)
        detail = exif.get_ifd(34665)
        self.assertEqual(detail[36867], '2020:04:05 06:07:08')
        self.assertEqual(detail[36881], '+08:00')
        self.assertEqual(detail[37521], '123')
        self.assertNotIn(33434, detail)
        self.assertEqual(exif.get_ifd(34853)[1], 'N')
        self.assertEqual(exif[274], 1)
        self.assertEqual((detail[40962], detail[40963]), (30, 40))

    def test_invalid_timestamp_or_unsupported_layout_never_changes_stage(self):
        original = self.movie.read_bytes()
        for time, duration in ((float('nan'), .1), (-.1, .1), (99, .1), (.1, 0)):
            with self.subTest(time=time, duration=duration), self.assertRaises(metadata.LivePhotoMetadataError):
                metadata.rewrite_movie(self.movie, self.identifier, time, duration)
            self.assertEqual(self.movie.read_bytes(), original)
        for invalid in (b'broken', metadata._box(b'moof', b'') + original, struct.pack('>I4s', 0, b'mdat')):
            self.movie.write_bytes(invalid)
            with self.assertRaises(metadata.LivePhotoMetadataError):
                metadata.rewrite_movie(self.movie, self.identifier, .1, .1)
            self.assertEqual(self.movie.read_bytes(), invalid)

    def test_cancel_before_metadata_write_leaves_stage_unchanged(self):
        original = self.movie.read_bytes()
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(InterruptedError):
            metadata.rewrite_movie(self.movie, self.identifier, .1, .1, cancel_event=cancelled)
        self.assertEqual(self.movie.read_bytes(), original)

    def test_corrupt_timed_sample_refuses_validation(self):
        self.encode()
        _, children = metadata._movie(self.movie)
        marker = next(payload for kind, payload in children if kind == b'trak' and metadata.STILL_TIME in payload)
        *_, stbl = metadata._track_info(marker)
        offset = struct.unpack_from('>Q', metadata._one(stbl, b'co64'), 8)[0]
        with self.movie.open('r+b') as stream:
            stream.seek(offset + 8); stream.write(b'\x01')
        with self.assertRaises(metadata.LivePhotoMetadataError):
            metadata.validate_pair(self.image, self.movie, self.identifier, .44)

    def test_audio_nonzero_pts_and_existing_udta_identifier_are_preserved(self):
        tools = resolve_frame_tools()
        # Synthetic tone and the public generated fixture; no private media.
        subprocess.run([str(tools.ffmpeg), '-v', 'error', '-nostdin', '-y',
            '-itsoffset', '0.5', '-i', str(FIXTURES / 'JPEG_PAIR.mov'),
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
            '-t', '2', '-movflags', 'use_metadata_tags', '-metadata',
            'com.apple.quicktime.content.identifier=OLD-PAIR', str(self.movie)],
            check=True, capture_output=True)
        before = essence(self.movie)
        self.assertTrue(any(packet['codec_type'] == 'audio' for packet in before))
        with FrameReader(self.movie) as reader:
            self.assertGreater(reader.metadata.start_time_seconds, .49)
            frame = reader.metadata.frames[3]
            seconds = frame.pts_seconds + reader.metadata.start_time_seconds
            image = reader.export(3)
        image.save(self.image, exif=metadata.make_photo_exif(self.source, self.identifier, image.size))
        metadata.rewrite_movie(self.movie, self.identifier, seconds, frame.duration_seconds)
        metadata.validate_pair(self.image, self.movie, self.identifier, seconds)
        self.assertEqual(essence(self.movie), before)
        packets = json.loads(subprocess.check_output([str(tools.ffprobe), '-v', 'error',
            '-select_streams', 'd', '-show_packets', '-of', 'json', str(self.movie)]))['packets']
        self.assertEqual(len(packets), 1)
        self.assertAlmostEqual(float(packets[0]['pts_time']), seconds, places=4)
        _, children = metadata._movie(self.movie)
        self.assertNotIn(b'OLD-PAIR', b''.join(payload for _, payload in children))

    def test_real_heic_preserves_apple_identifier_after_full_decode(self):
        from livegallery.heic_encoder import _executable, encode_heic
        try:
            _executable()
        except OSError:
            self.skipTest('HEIC component is built before the mandatory packaged self-test')
        frame = self.encode()
        target = self.root / 'live.heic'
        with Image.open(self.image) as image:
            encode_heic(image, target, exif=metadata.make_photo_exif(self.source, self.identifier, image.size))
        with Image.open(target) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (320, 240))
        metadata.validate_pair(target, self.movie, self.identifier, frame.pts_seconds)


if __name__ == '__main__':
    unittest.main()
