import os
import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import av
import numpy as np
from PIL import Image, ImageDraw

from livegallery.cache import CacheStore
from livegallery.imaging import load_image, thumbnail_bytes
from livegallery.library import scan_folder
from livegallery.matching import ALGORITHM_VERSION, _mov_identifiers, close_video_probe, resolve_pair


def pattern(kind=0):
    image = Image.new("RGB", (160, 120), (20, 60, 180) if kind == 0 else (190, 65, 10))
    draw = ImageDraw.Draw(image)
    for index in range(8):
        left = index * 20
        draw.rectangle((left, 10 + index * 7, left + 12, 90), fill=(210 - index * 15, 220, 30) if kind == 0 else (20, 25 + index * 25, 180))
    draw.ellipse((30, 30, 90, 90), fill=(240, 30, 140) if kind == 0 else (40, 235, 85))
    return image


def write_movie(path, image):
    with av.open(str(path), mode="w") as output:
        stream = output.add_stream("mpeg4", rate=12)
        stream.width, stream.height = image.size
        stream.pix_fmt = "yuv420p"
        for index in range(24):
            frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = CacheStore(self.root / "cache")

    def tearDown(self):
        close_video_probe()
        self.cache.close()
        self.temporary.cleanup()

    def save(self, name, image):
        path = self.root / name
        # PNG payloads under HEIC extension isolate candidate-selection logic;
        # actual HEIC decoder fixtures are verified separately by packaging QA.
        image.save(path, format="PNG" if path.suffix == ".heic" else "JPEG", quality=98)
        return path

    def live(self):
        return next(item for item in scan_folder(self.root) if item.is_live)

    def test_equivalent_jpg_jpeg_heic_chooses_heic_and_hides_duplicates(self):
        for suffix in ("jpg", "jpeg", "heic"):
            self.save("pair." + suffix, pattern())
        (self.root / "pair.mov").touch()
        decision = resolve_pair(self.live(), self.cache)
        self.assertEqual(decision.image_path.suffix, ".heic")
        self.assertEqual(decision.extra_images, ())
        self.assertTrue(decision.same_content)
        self.assertFalse(decision.uncertain)

    def test_distinct_images_select_matching_video_and_preserve_other_content(self):
        heic = self.save("pair.heic", pattern(0))
        jpeg = self.save("pair.jpeg", pattern(1))
        write_movie(self.root / "pair.mov", pattern(1))
        decision = resolve_pair(self.live(), self.cache)
        self.assertEqual(decision.image_path, jpeg)
        self.assertEqual(decision.extra_images, (heic,))
        self.assertFalse(decision.uncertain)

    def test_unrelated_candidates_and_video_are_uncertain(self):
        self.save("pair.heic", pattern(0))
        self.save("pair.jpg", pattern(1))
        write_movie(self.root / "pair.mov", Image.new("RGB", (160, 120), (240, 240, 240)))
        decision = resolve_pair(self.live(), self.cache)
        self.assertTrue(decision.uncertain)
        self.assertIn("配对可能不正确", decision.reason)

    def test_video_decode_failure_prefers_heic_but_marks_uncertain(self):
        heic = self.save("pair.heic", pattern(0))
        self.save("pair.jpg", pattern(1))
        (self.root / "pair.mov").write_bytes(b"not a movie")
        decision = resolve_pair(self.live(), self.cache)
        self.assertEqual(decision.image_path, heic)
        self.assertTrue(decision.uncertain)
        self.assertEqual(len(decision.extra_images), 1)

    def test_resolution_preference_for_same_jpg_and_jpeg(self):
        self.save("pair.jpg", pattern().resize((320, 240)))
        high = self.save("pair.jpeg", pattern().resize((1280, 960)))
        (self.root / "pair.mov").touch()
        decision = resolve_pair(self.live(), self.cache)
        self.assertEqual(decision.image_path, high)

    def test_cache_reuses_results_and_invalidates_for_file_and_algorithm_changes(self):
        self.save("pair.jpg", pattern())
        heic = self.save("pair.heic", pattern())
        movie = self.root / "pair.mov"
        movie.touch()
        item = self.live()
        first = resolve_pair(item, self.cache)
        with patch("livegallery.matching.load_image", side_effect=AssertionError("Should use cached result")):
            self.assertEqual(resolve_pair(item, self.cache), first)
        self.assertIsNone(self.cache.get_decision(item, "different-algorithm"))
        stat = heic.stat()
        os.utime(heic, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10000000))
        self.assertIsNone(self.cache.get_decision(item, ALGORITHM_VERSION))
        resolve_pair(item, self.cache)
        movie.write_bytes(b"modified")
        self.assertIsNone(self.cache.get_decision(item, ALGORITHM_VERSION))

    def test_manual_choice_persists_and_is_not_overwritten(self):
        heic = self.save("pair.heic", pattern(0))
        jpeg = self.save("pair.jpg", pattern(1))
        write_movie(self.root / "pair.mov", pattern(1))
        item = self.live()
        self.cache.set_override(item, heic)
        self.assertEqual(resolve_pair(item, self.cache).image_path, heic)
        self.cache.forget_decision(item)
        self.assertEqual(self.cache.get_override(item), heic)
        self.cache.set_override(item, None)
        self.assertEqual(resolve_pair(item, self.cache).image_path, jpeg)

    def test_thread_safe_atomic_thumbnail_creation_and_file_invalidation(self):
        path = self.save("plain.jpg", pattern())
        with ThreadPoolExecutor(max_workers=8) as executor:
            paths = list(executor.map(lambda _: self.cache.get_thumbnail(path), range(24)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(load_image(paths[0]).mode, "RGB")
        self.save("plain.jpg", pattern(1))
        self.assertNotEqual(self.cache.get_thumbnail(path), paths[0])
        self.assertFalse(list(self.cache.thumbnail_dir.glob("*.tmp")))

    def test_thumbnail_cache_size_is_bounded(self):
        first = self.save("one.jpg", pattern())
        second = self.save("two.jpg", pattern(1))
        bound = max(len(thumbnail_bytes(first, 64)), len(thumbnail_bytes(second, 64))) + 1
        cache = CacheStore(self.root / "bounded", max_bytes=bound)
        try:
            cache.get_thumbnail(first, 64)
            current = cache.get_thumbnail(second, 64)
            self.assertTrue(current.exists())
            self.assertLessEqual(sum(path.stat().st_size for path in cache.thumbnail_dir.glob("*.jpg")), bound)
        finally:
            cache.close()
        # Lowering a configured limit also applies to a pre-existing cache.
        reopened = CacheStore(self.root / "bounded", max_bytes=0)
        try:
            self.assertEqual(list(reopened.thumbnail_dir.glob("*.jpg")), [])
        finally:
            reopened.close()

    def test_mov_identifier_reads_metadata_and_skips_media_payload(self):
        media_id = b"01234567-89AB-CDEF-0123-456789ABCDEF"
        fake_media_id = b"11111111-1111-1111-1111-111111111111"
        path = self.root / "metadata.mov"
        mdat = struct.pack(">I4s", len(fake_media_id) + 8, b"mdat") + fake_media_id
        moov = struct.pack(">I4s", len(media_id) + 8, b"moov") + media_id
        path.write_bytes(mdat + moov)
        self.assertEqual(_mov_identifiers(path), {media_id.decode().casefold()})

    def test_clear_thumbnails_keeps_manual_choices_and_analysis(self):
        selected = self.save("pair.heic", pattern())
        self.save("pair.jpg", pattern())
        (self.root / "pair.mov").touch()
        item = self.live()
        self.cache.set_override(item, selected)
        decision = resolve_pair(item, self.cache)
        thumbnail = self.cache.get_thumbnail(selected)
        self.assertTrue(thumbnail.exists())
        reclaimed = self.cache.clear_thumbnails()
        self.assertGreater(reclaimed, 0)
        self.assertFalse(thumbnail.exists())
        self.assertEqual(self.cache.get_override(item), selected)
        self.assertEqual(self.cache.get_decision(item, ALGORITHM_VERSION), decision)
        self.assertTrue(self.cache.get_thumbnail(selected).exists())

    def test_cache_close_is_idempotent(self):
        cache = CacheStore(self.root / "closing")
        cache.close()
        cache.close()

    def test_image_orientation_alpha_and_animation_first_frame(self):
        exif = Image.Exif()
        exif[274] = 6
        photo = self.root / "oriented.jpg"
        pattern().save(photo, exif=exif)
        self.assertEqual(load_image(photo).size, (120, 160))
        transparent = self.root / "transparent.png"
        Image.new("RGBA", (4, 4), (255, 0, 0, 0)).save(transparent)
        self.assertEqual(load_image(transparent).getpixel((0, 0)), (255, 255, 255))
        animation = self.root / "animated.gif"
        Image.new("RGB", (4, 4), "red").save(animation, save_all=True,
                                               append_images=[Image.new("RGB", (4, 4), "blue")], duration=100, loop=0)
        self.assertEqual(load_image(animation).getpixel((0, 0)), (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
