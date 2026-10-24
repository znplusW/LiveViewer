import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
