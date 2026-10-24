"""Exact video-frame identity, VFR timestamps, orientation and cancellation."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import av
from PIL import ImageChops, ImageColor, ImageStat

from livegallery.frame_reader import FrameCancelled, FrameReader
from livegallery.frame_tools import resolve_frame_tools


FIXTURES = Path(__file__).resolve().parent / 'fixtures'
VFR_TIMES = (0, 40, 160, 200, 440, 480, 760)
VFR_COLORS = ('#d43d35', '#42bd51', '#3568d4', '#e2d63c', '#cf42c2', '#3fc2d1', '#df8d32')


class ExactFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            resolve_frame_tools()
        except OSError as error:
            raise unittest.SkipTest(str(error))

    def test_variable_rate_frames_have_exact_indices_and_timestamps(self):
        with FrameReader(FIXTURES / 'VFR_PAIR.MOV') as reader:
            self.assertEqual(reader.frame_count, len(VFR_TIMES))
            self.assertFalse(reader.metadata.timestamps_estimated)
            self.assertEqual([round(frame.pts_seconds * 1000) for frame in reader.metadata.frames], list(VFR_TIMES))
            self.assertEqual(reader.nearest_index(140), 2)
            self.assertEqual(reader.nearest_index(9999), 6)
            self.assertEqual(reader.nearest_index(-10), 0)
            for index, color in enumerate(VFR_COLORS):
                image = reader.export(index)
                actual = image.getpixel((160, 120))
                expected = ImageColor.getrgb(color)
                self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 8)

    def test_h264_and_hevc_frames_match_independent_decoded_frame_order(self):
        for name in ('Live_H264.MOV', 'Live_HEVC.MOV'):
            path = FIXTURES / name
            with av.open(str(path)) as container:
                expected = [frame.to_image().convert('RGB') for frame in container.decode(video=0)]
            with FrameReader(path) as reader:
                self.assertEqual(reader.frame_count, len(expected))
                for index in (0, 9, len(expected) - 1):
                    actual = reader.export(index)
                    self.assertEqual(actual.size, expected[index].size)
                    error = ImageStat.Stat(ImageChops.difference(actual, expected[index])).mean
                    self.assertLess(max(error), 3, f'{name} frame {index}: {error}')
                preview = reader.preview(9, edge=160)
                self.assertLessEqual(max(preview.size), 160)
                self.assertEqual(reader.export(9).size, (640, 420))

    def test_rotated_hevc_export_is_portrait_and_has_expected_corner_order(self):
        with FrameReader(FIXTURES / 'Portrait_HEVC.MOV') as reader:
            self.assertEqual((reader.metadata.width, reader.metadata.height), (420, 640))
            image = reader.export(0)
            self.assertEqual(image.size, (420, 640))
            green, yellow, red, blue = [image.getpixel((round(image.width * x), round(image.height * y)))
                                        for x, y in ((.04, .04), (.96, .04), (.04, .96), (.96, .96))]
            self.assertGreater(green[1], max(green[0], green[2]) + 40)
            self.assertGreater(min(yellow[:2]), yellow[2] + 60)
            self.assertGreater(red[0], max(red[1:]) + 40)
            self.assertGreater(blue[2], max(blue[:2]) + 40)

    def test_invalid_indices_source_change_and_reader_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'copy.mov'
            shutil.copy2(FIXTURES / 'Live_H264.MOV', path)
            reader = FrameReader(path)
            reader.open()
            with self.assertRaises(IndexError):
                reader.export(reader.frame_count)
            with self.assertRaises(IndexError):
                reader.preview(-1)
            reader.cancel_current()
            self.assertEqual(reader.preview(0, 100).width, 100)
            path.write_bytes(path.read_bytes() + b'changed')
            with self.assertRaisesRegex(OSError, '已变化'):
                reader.preview(0)
            reader.close()
            with self.assertRaises(FrameCancelled):
                reader.open()

    def test_persistent_cancel_event_can_be_cleared_for_a_new_request(self):
        event = threading.Event()
        reader = FrameReader(FIXTURES / 'Live_H264.MOV', cancel_event=event)
        event.set()
        with self.assertRaises(FrameCancelled):
            reader.open()
        event.clear()
        try:
            self.assertEqual(reader.frame_count, 21)
        finally:
            reader.close()


class ProcessBoundsTests(unittest.TestCase):
    def test_active_request_is_cancelled_without_poisoning_next_request(self):
        reader = FrameReader(FIXTURES / 'Live_H264.MOV')
        failures = []
        def worker():
            try:
                reader._run([sys.executable, '-c', 'import time; time.sleep(5)'], 1024)
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 2
        while reader._process is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNotNone(reader._process)
        reader.cancel_current()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], FrameCancelled)
        self.assertEqual(reader._run([sys.executable, '-c', 'print("ready")'], 1024).strip(), b'ready')
        reader.close()

    def test_child_output_and_timeout_are_bounded(self):
        reader = FrameReader(FIXTURES / 'Live_H264.MOV')
        with self.assertRaises(ValueError):
            reader._run([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x" * 10000)'], 1024)
        with patch('livegallery.frame_reader.PROCESS_TIMEOUT_SECONDS', .15):
            with self.assertRaises(TimeoutError):
                reader._run([sys.executable, '-c', 'import time; time.sleep(5)'], 1024)
        self.assertIsNone(reader._process)
        reader.close()


if __name__ == '__main__':
    unittest.main()
