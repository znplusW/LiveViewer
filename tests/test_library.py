import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from livegallery.cache_locations import (CACHE_DIRECTORY, MANIFEST_NAME, OWNERSHIP_SENTINEL,
                                         PRODUCT, SCHEMA_VERSION)
from livegallery.library import scan_folder


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_recursive_pairing_stays_in_same_directory_and_accepts_jpeg(self):
        self.file("A/IMG_2.JPEG")
        self.file("A/img_2.MoV")
        self.file("B/IMG_2.jpg")
        self.file("C/IMG_2.mov")
        items = scan_folder(self.root)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].is_live)
        self.assertEqual(items[0].video_path.parent, items[0].image_path.parent)
        self.assertFalse(items[1].is_live)

    def test_ordinary_formats_never_join_pair_candidates(self):
        for suffix in ("jpg", "HEIC", "JPEG", "png", "WEBP", "bmp", "gif", "tif", "tiff", "avif", "MOV"):
            self.file("test." + suffix)
        items = scan_folder(self.root)
        live = next(item for item in items if item.is_live)
        self.assertEqual(live.image_path.suffix, ".HEIC")
        self.assertEqual(len(live.candidates), 3)
        self.assertTrue(live.pending)
        self.assertEqual(len(items), 8)
        self.assertTrue(all(not item.is_live for item in items if item is not live))

    def test_no_video_retains_every_static_candidate(self):
        for suffix in ("jpg", "jpeg", "heic"):
            self.file("test." + suffix)
        self.assertEqual(len(scan_folder(self.root)), 3)

    def test_natural_order_and_progress(self):
        for name in ("10.jpg", "2.jpg", "1.jpg"):
            self.file(name)
        counts = []
        self.assertEqual([item.image_path.name for item in scan_folder(self.root, progress=counts.append)],
                         ["1.jpg", "2.jpg", "10.jpg"])
        self.assertEqual(counts[-1], 3)

    def test_does_not_follow_symlink_directories(self):
        self.file("real/test.png")
        try:
            (self.root / "alias").symlink_to(self.root / "real", target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        self.assertEqual(len(scan_folder(self.root)), 1)

    def test_cancel_before_scan(self):
        self.file("test.jpg")
        self.assertEqual(scan_folder(self.root, cancel=lambda: True), [])

    def test_excludes_editor_staging_directories_at_every_level(self):
        self.file("visible.jpg")
        self.file(".LiveViewer-work/staged.HEIC")
        self.file("nested/.liveviewer-WORK/staged.jpg")
        self.file(".normal-hidden/retained.png")
        self.assertEqual({item.image_path.name for item in scan_folder(self.root)},
                         {"visible.jpg", "retained.png"})
        self.assertEqual(scan_folder(self.root / ".LiveViewer-work"), [])

    def cache_directory(self, relative, *, product=PRODUCT, schema=SCHEMA_VERSION):
        cache = self.root / relative
        cache.mkdir(parents=True, exist_ok=True)
        (cache / MANIFEST_NAME).write_text(json.dumps({
            "product": product, "schema": schema, "library_id": str(uuid.uuid4()),
            "thumbnail_format": 3,
        }), encoding="utf-8")
        (cache / "cached.jpg").touch()
        return cache

    def test_managed_library_caches_are_excluded_at_root_and_nested_levels(self):
        self.file("actual.jpg")
        self.file("actual.mov")
        self.file("nested/second.png")
        root_cache = self.cache_directory(CACHE_DIRECTORY)
        self.cache_directory("nested/" + CACHE_DIRECTORY.upper())
        items = scan_folder(self.root)
        self.assertEqual({item.image_path.name for item in items}, {"actual.jpg", "second.png"})
        self.assertEqual(len([item for item in items if item.is_live]), 1)
        self.assertEqual(scan_folder(root_cache), [])

    def test_same_named_ordinary_folder_and_foreign_marker_are_still_browsable(self):
        self.file(CACHE_DIRECTORY + "/family.jpg")
        self.cache_directory("foreign/" + CACHE_DIRECTORY, product="OtherPhotoApplication")
        items = scan_folder(self.root)
        self.assertEqual({item.image_path.name for item in items}, {"family.jpg", "cached.jpg"})

    def test_recognized_future_cache_schema_is_not_browsed_as_photos(self):
        self.file("actual.webp")
        self.cache_directory(CACHE_DIRECTORY, schema=SCHEMA_VERSION + 100)
        self.assertEqual([item.image_path.name for item in scan_folder(self.root)], ["actual.webp"])

    def test_malformed_marker_does_not_hide_an_ordinary_same_named_photo_folder(self):
        self.file(CACHE_DIRECTORY + "/actual.jpeg")
        (self.root / CACHE_DIRECTORY / MANIFEST_NAME).write_text("not JSON", encoding="utf-8")
        self.assertEqual([item.image_path.name for item in scan_folder(self.root)], ["actual.jpeg"])

    def test_known_cache_stays_excluded_with_damaged_manifest_but_foreign_sentinel_does_not_hide_photos(self):
        known = self.cache_directory(CACHE_DIRECTORY)
        (known / OWNERSHIP_SENTINEL).write_bytes((PRODUCT + "\n").encode("ascii"))
        (known / MANIFEST_NAME).write_text("damaged JSON", encoding="utf-8")
        foreign = self.file("foreign/" + CACHE_DIRECTORY + "/actual.jpeg").parent
        (foreign / OWNERSHIP_SENTINEL).write_bytes(b"OtherPhotoApplication\n")
        self.assertEqual([item.image_path.name for item in scan_folder(self.root)], ["actual.jpeg"])
        self.assertEqual(scan_folder(known), [])

    def test_nonrecursive_scan_does_not_enter_subdirectories(self):
        self.file("root.jpeg")
        self.file("root.mov")
        self.file("nested/photo.jpg")
        self.file("nested/photo.mov")
        self.file("nested/deeper/plain.png")
        self.assertEqual(len(scan_folder(self.root)), 3)
        items = scan_folder(self.root, recursive=False)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].is_live)
        self.assertEqual(items[0].image_path.name, "root.jpeg")

    def test_excluded_branches_are_pruned_before_scanning_and_never_modify_files(self):
        keep = self.file('keep/photo.jpg')
        gone = self.file('removed/child/pair.jpeg')
        movie = self.file('removed/child/pair.mov')
        for path in (keep, gone, movie):
            path.write_bytes(b'original immutable fixture ' + path.name.encode())
        original = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (keep, gone, movie)}
        original_scandir = os.scandir
        def guarded_scandir(path):
            self.assertFalse(Path(path).is_relative_to(self.root / 'removed'))
            return original_scandir(path)
        with patch('livegallery.library.os.scandir', side_effect=guarded_scandir):
            indexed = scan_folder(self.root, excluded=(self.root / 'removed',))
        self.assertEqual([item.image_path for item in indexed], [keep])
        self.assertEqual(scan_folder(self.root / 'removed', excluded=(self.root / 'removed',)), [])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in original}, original)


if __name__ == "__main__":
    unittest.main()
