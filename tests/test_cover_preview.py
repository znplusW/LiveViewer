"""Progressive previews, sustained input, bounded RAM and honest cover marks."""
from pathlib import Path
from fractions import Fraction
from types import SimpleNamespace
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageStat

from livegallery.cover_preview import PreviewSession, MAX_LONG_SAMPLES, _cover_signatures, _cover_similarity
from livegallery.frame_reader import FrameCancelled, FrameReader
from livegallery.frame_tools import resolve_frame_tools
from livegallery.matching import _similarity
from tests.test_frame_reader import _add_clean_aperture


FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def metadata(count=100, width=120, height=90):
    return SimpleNamespace(width=width, height=height,
                           frames=tuple(SimpleNamespace(index=i, pts_seconds=i * .04) for i in range(count)))


class SlowReader:
    instances = []

    def __init__(self, path, *, cancel_event=None, **kwargs):
        self.cancel_event = cancel_event
        self.cancel_calls = 0
        self.indices = []
        self.instances.append(self)

    def preview(self, index, edge=720):
        self.indices.append(index)
        if self.cancel_event.wait(.04):
            raise FrameCancelled('closed')
        return Image.new('RGB', (120, 90), (index % 255, 40, 60))

    def cancel_current(self):
        self.cancel_calls += 1

    def close(self):
        self.cancel_current()


def no_preload(session):
    with session._condition:
        session._stream_done = True
        session._condition.notify_all()


class PreviewSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'source.MOV'
        self.path.write_bytes(b'video source placeholder')
        SlowReader.instances.clear()

    def close(self, session):
        session.close()
        self.assertTrue(session.wait(3))

    def test_uncached_preview_advances_while_input_keeps_changing(self):
        shown = []
        stopped_input = threading.Event()
        final = threading.Event()
        def frame(index, image):
            shown.append((index, stopped_input.is_set(), image.getpixel((0, 0))))
            if index == 99:
                final.set()
        with patch('livegallery.cover_preview.FrameReader', SlowReader), \
                patch.object(PreviewSession, '_preload', no_preload):
            session = PreviewSession(self.path, metadata(), on_frame=frame, max_bytes=64 * 1024)
            self.addCleanup(self.close, session)
            session.start()
            for index in range(100):
                session.request(index)
                time.sleep(.005)
            stopped_input.set()
            self.assertTrue(final.wait(2), shown)
            self.assertGreaterEqual(len([entry for entry in shown if not entry[1]]), 4, shown)
            self.assertGreater(len({entry[0] for entry in shown}), 5)
            self.assertEqual(shown[-1][0], 99)
            self.assertTrue(all(entry[2][0] == entry[0] for entry in shown))
            self.assertEqual(SlowReader.instances[0].cancel_calls, 0)
            self.assertLessEqual(session.cache_bytes, 64 * 1024)

    def test_long_video_warm_plan_is_sampled_and_cache_respects_packed_budget(self):
        session = PreviewSession(self.path, metadata(20_000, 3840, 2160), max_bytes=1024 * 1024)
        self.assertLessEqual(len(session._sample_indices), MAX_LONG_SAMPLES)
        self.assertGreater(session._sample_indices[1], 1)
        for index in range(200):
            session._store(index, Image.new('RGB', (200, 100), 'blue'))
            self.assertLessEqual(session.cache_bytes, 1024 * 1024)
        self.assertLess(len(session.cached_indices), 20)
        image = session.get(199)
        image.putpixel((0, 0), (255, 0, 0))
        self.assertEqual(session.get(199).getpixel((0, 0)), (0, 0, 255))
        session.close()

    def test_stabilised_video_field_of_view_matches_current_still_without_lower_threshold(self):
        still = Image.new('RGB', (640, 480), '#576576')
        draw = ImageDraw.Draw(still)
        colours = ['#d13b41', '#427bc1', '#e9b73d', '#65aa53', '#b655b5']
        for row in range(6):
            for column in range(8):
                draw.rectangle((column * 80 + 8, row * 80 + 8,
                                column * 80 + 72, row * 80 + 72),
                               fill=colours[(row * 3 + column) % len(colours)])
        # Same aspect ratio, narrower video FOV: aspect-only matching misses it.
        frame = still.crop((32, 24, 608, 456)).resize((160, 120))
        self.assertLess(_similarity(still, frame), .72)
        self.assertGreater(_cover_similarity(_cover_signatures(still, 4 / 3), frame), .97)

    def test_callbacks_are_outside_cache_lock_and_close_stops_worker(self):
        callback_done = threading.Event()
        with patch('livegallery.cover_preview.FrameReader', SlowReader), \
                patch.object(PreviewSession, '_preload', no_preload):
            session = PreviewSession(self.path, metadata())
            def callback(index, image):
                inspected = threading.Event()
                thread = threading.Thread(target=lambda: (session.get(index), inspected.set()))
                thread.start()
                self.assertTrue(inspected.wait(1), 'callback held cache lock')
                thread.join()
                callback_done.set()
            session.on_frame = callback
            session.start()
            session.request(1)
            self.assertTrue(callback_done.wait(2))
            start = time.monotonic()
            self.close(session)
            self.assertLess(time.monotonic() - start, 1)

    def test_requested_warming_frame_is_published_while_fallback_is_busy(self):
        entered, gate, visible = threading.Event(), threading.Event(), threading.Event()
        class BlockedReader(SlowReader):
            def preview(self, index, edge=720):
                entered.set()
                while not gate.wait(.01):
                    if self.cancel_event.is_set():
                        raise FrameCancelled('closed')
                return super().preview(index, edge)
        with patch('livegallery.cover_preview.FrameReader', BlockedReader), \
                patch.object(PreviewSession, '_preload', no_preload):
            session = PreviewSession(self.path, metadata(), on_frame=lambda i, image: visible.set())
            self.addCleanup(self.close, session)
            session.start()
            session.request(5)
            self.assertTrue(entered.wait(1))
            session._store(5, Image.new('RGB', (120, 90), 'blue'), matching=True)
            self.assertTrue(visible.wait(.2), 'warm cache waited for the cold seek')
            self.assertFalse(gate.is_set())
            gate.set()

    def test_changed_video_reports_error_and_future_requests_can_recover(self):
        errors, frames = [], []
        with patch('livegallery.cover_preview.FrameReader', SlowReader), \
                patch.object(PreviewSession, '_preload', no_preload):
            session = PreviewSession(self.path, metadata(), on_error=errors.append,
                                     on_frame=lambda index, image: frames.append(index))
            self.addCleanup(self.close, session)
            session.start()
            self.path.write_bytes(b'external change')
            session.request(1)
            deadline = time.monotonic() + 2
            while not errors and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(errors)
            self.assertIn('已变化', errors[0])
            self.assertFalse(frames)
            # Updating a signature is a test-only analogue of reopening the
            # reader after reconnect. A target failure did not kill the worker.
            session._signature = session._file_signature(self.path)
            session.request(2)
            deadline = time.monotonic() + 2
            while not frames and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(frames, [2])


class RealPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            resolve_frame_tools()
        except OSError as error:
            raise unittest.SkipTest(str(error))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def session(self, path, **kwargs):
        with FrameReader(path) as reader:
            info = reader.metadata
        session = PreviewSession(path, info, **kwargs)
        self.addCleanup(lambda: self.assertTrue(session.wait(3)))
        self.addCleanup(session.close)
        session.start()
        return session

    def wait_stream(self, session):
        deadline = time.monotonic() + 5
        while not session.preload_complete and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(session.preload_complete)
        self.assertIsNone(session.preload_error)

    def test_vfr_stream_matches_each_exact_frame_and_timestamps(self):
        path = FIXTURES / 'VFR_PAIR.MOV'
        session = self.session(path)
        self.wait_stream(session)
        self.assertEqual(set(session.cached_indices), set(range(7)))
        with FrameReader(path) as reader:
            for index in range(7):
                image = session.get(index)
                expected = reader.export(index)
                self.assertEqual(image.size, expected.size)
                error = ImageStat.Stat(ImageChops.difference(image, expected)).mean
                self.assertLess(max(error), 2)
        self.assertEqual(round(session.metadata.frames[4].pts_seconds * 1000), 440)

    def test_held_input_uses_warmed_frames_before_release(self):
        shown = []
        stopped = threading.Event()
        session = self.session(FIXTURES / 'VFR_PAIR.MOV',
                               on_frame=lambda index, image: shown.append((index, stopped.is_set())))
        self.wait_stream(session)
        for index in list(range(7)) * 3:
            session.request(index)
            time.sleep(.015)
        stopped.set()
        self.assertGreaterEqual(len([index for index, after in shown if not after]), 15, shown)
        self.assertEqual({index for index, after in shown if not after}, set(range(7)))

    def test_cold_real_session_advances_during_continuous_requests(self):
        shown, stopped = [], threading.Event()
        session = self.session(FIXTURES / 'Portrait_HEVC.MOV',
                               on_frame=lambda index, image: shown.append((index, stopped.is_set())))
        for index in list(range(21)) * 3:
            session.request(index)
            time.sleep(.015)
        stopped.set()
        self.assertGreaterEqual(len([index for index, after in shown if not after]), 15, shown)
        self.assertGreaterEqual(len({index for index, after in shown if not after}), 10, shown)

    def test_current_image_match_changes_after_cover_is_replaced(self):
        path = FIXTURES / 'VFR_PAIR.MOV'
        still = self.folder / 'current.jpg'
        for selected in (4, 1):
            with FrameReader(path) as reader:
                reader.export(selected).save(still, 'JPEG', quality=95)
            positions = []
            found = threading.Event()
            def position(value):
                positions.append(value)
                found.set()
            session = self.session(path, image_path=still, on_position=position)
            self.assertTrue(found.wait(5), session.preload_error)
            self.assertEqual(positions[0].index, selected)
            self.assertAlmostEqual(positions[0].seconds, session.metadata.frames[selected].pts_seconds)
            self.assertTrue(positions[0].approximate)
            self.assertGreater(positions[0].confidence, .9)
            session.close()
            self.assertTrue(session.wait(3))

    def test_exact_marker_requires_matching_image_identifier_else_visual_fallback(self):
        from livegallery.live_photo_image import build_cover_exif
        from livegallery.live_photo_movie import rewrite_live_photo_movie
        path, still = self.folder / 'marked.MOV', self.folder / 'marked.jpg'
        rewrite_live_photo_movie(FIXTURES / 'VFR_PAIR.MOV', path, 'known-pair', Fraction(11, 25))
        # Image pixels intentionally match frame 1, distinct from marker frame4.
        with FrameReader(path) as reader:
            frame = reader.export(1)
        for image_id, expected, approximate in (('known-pair', 4, False), ('other-pair', 1, True), (None, 1, True)):
            with self.subTest(image_id=image_id):
                frame.save(still, 'JPEG', quality=95)
                if image_id is not None:
                    frame.save(still, 'JPEG', quality=95, exif=build_cover_exif(still, image_id, frame.size))
                positions, found = [], threading.Event()
                session = self.session(path, image_path=still,
                                       on_position=lambda value: (positions.append(value), found.set()))
                self.assertTrue(found.wait(5), session.preload_error)
                self.assertEqual(positions[0].index, expected)
                self.assertEqual(positions[0].approximate, approximate)
                session.close()
                self.assertTrue(session.wait(3))

    def test_exact_marker_does_not_ignore_changed_still_signature(self):
        from livegallery.live_photo_image import build_cover_exif
        from livegallery.live_photo_movie import rewrite_live_photo_movie
        path, still = self.folder / 'marked.MOV', self.folder / 'marked.jpg'
        rewrite_live_photo_movie(FIXTURES / 'VFR_PAIR.MOV', path, 'known-pair', Fraction(11, 25))
        frame = Image.new('RGB', (120, 90), 'blue')
        frame.save(still)
        frame.save(still, exif=build_cover_exif(still, 'known-pair', frame.size))
        with FrameReader(path) as reader:
            info = reader.metadata
        positions = []
        session = PreviewSession(path, info, image_path=still, on_position=positions.append)
        self.addCleanup(session.close)
        Image.new('RGB', (40, 30), 'red').save(still)
        session._match_position()
        self.assertEqual(positions, [])

    def test_unmatched_cover_does_not_silently_mark_frame_zero(self):
        still = self.folder / 'other.jpg'
        Image.new('RGB', (200, 100), 'black').save(still)
        positions, found = [], threading.Event()
        session = self.session(FIXTURES / 'VFR_PAIR.MOV', image_path=still,
                               on_position=lambda value: (positions.append(value), found.set()))
        self.assertTrue(found.wait(5), session.preload_error)
        self.assertIsNone(positions[0].index)
        self.assertTrue(positions[0].ambiguous)

    def test_portrait_container_crop_has_the_actual_preview_aspect(self):
        path = self.folder / 'cropped.mov'
        _add_clean_aperture(FIXTURES / 'Portrait_HEVC.MOV', path, 608, 388)
        session = self.session(path, edge=240)
        self.wait_stream(session)
        image = session.get(0)
        self.assertEqual((session.metadata.width, session.metadata.height), (388, 608))
        self.assertEqual(image.size, (153, 240))
        with FrameReader(path) as reader:
            expected = reader.export(0).resize(image.size, Image.Resampling.BILINEAR)
        error = ImageStat.Stat(ImageChops.difference(image, expected)).mean
        self.assertLess(max(error), 4)

    def test_real_cache_budget_and_close_remain_bounded(self):
        session = self.session(FIXTURES / 'Live_H264.MOV', max_bytes=48 * 1024)
        self.wait_stream(session)
        self.assertLessEqual(session.cache_bytes, 48 * 1024)
        self.assertEqual(len(session.cached_indices), 21)
        session.close()
        self.assertTrue(session.wait(3))


if __name__ == '__main__':
    unittest.main()
