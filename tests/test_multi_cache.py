"""Merged-library cache routing, ownership, migration and bounded storage."""
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from livegallery.cache import CacheStore
from livegallery.cache_locations import CACHE_DIRECTORY, open_cache
from livegallery.library import scan_folder
from livegallery.models import MediaItem, PairDecision
from livegallery.multi_cache import (MultiRootCache, normalise_roots, open_multi_cache,
                                    scan_roots)


class MultiCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.parent = self.base / 'parent'
        self.child = self.parent / 'child'
        self.other = self.base / 'other'
        self.roots = (self.parent, self.child, self.other)
        for root in self.roots:
            root.mkdir(parents=True, exist_ok=True)
        self.user, self.program = self.base / 'user', self.base / 'program'
        self.bindings = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for binding in reversed(self.bindings):
            binding.close()

    def open(self, mode='library', roots=None, **kwargs):
        binding = open_multi_cache(mode, self.roots if roots is None else roots,
                                   user_base=self.user, program_base=self.program, **kwargs)
        self.bindings.append(binding)
        return binding

    def pairs(self):
        output = []
        for root, color in zip(self.roots, ('red', 'green', 'blue')):
            for suffix in ('.jpg', '.jpeg'):
                Image.new('RGB', (120, 90), color).save(root / ('same' + suffix), 'JPEG')
            (root / 'same.mov').write_bytes(b'same movie fixture')
            output.append(next(item for item in scan_folder(root, recursive=False) if item.is_live))
        return output

    def test_normalize_keeps_nested_selections_and_scan_roots_avoids_repeat_walks(self):
        values = (self.child, self.parent, self.parent / '.', self.other,
                  self.child / '..' / 'child')
        self.assertEqual(normalise_roots(values), tuple(path.resolve() for path in (self.child, self.parent, self.other)))
        self.assertEqual(scan_roots(values), tuple(path.resolve() for path in (self.parent, self.other)))
        self.assertEqual(scan_roots(values, recursive=False), tuple(path.resolve() for path in (self.child, self.parent, self.other)))

    def test_single_root_returns_original_binding_and_store(self):
        binding = self.open(roots=(self.parent, self.parent / '.'))
        self.assertIsInstance(binding.cache, CacheStore)
        self.assertNotIsInstance(binding.cache, MultiRootCache)
        self.assertEqual(binding.cache.library_root, self.parent)
        self.assertEqual(binding.actual_path, self.parent / CACHE_DIRECTORY)

    def test_three_root_dispatch_uses_deepest_root_and_preserves_separate_choices(self):
        items = self.pairs()
        binding = self.open()
        cache = binding.cache
        self.assertEqual(cache.writer_roots, self.roots)
        self.assertEqual(len(cache.actual_paths), 3)
        self.assertIsNone(cache.library_root)
        for number, (root, item) in enumerate(zip(self.roots, items)):
            self.assertEqual(cache.root_for(item), root)
            self.assertEqual(cache.base_for(item), root / CACHE_DIRECTORY)
            selected = item.candidates[number % 2]
            cache.set_override(item, selected)
            self.assertEqual(cache.get_override(item), selected)
            decision = PairDecision(selected, (), False, str(number))
            cache.put_decision(item, 'alg', decision)
            self.assertEqual(cache.get_decision(item, 'alg'), decision)
            thumb = cache.get_thumbnail(selected)
            self.assertEqual(thumb.parent.parent, root / CACHE_DIRECTORY)
        self.assertNotEqual(cache.get_override(items[0]), cache.get_override(items[1]))
        with self.assertRaises(ValueError):
            cache.get_thumbnail(self.base / 'not-selected.jpg')

    def test_library_budgets_sum_to_one_limit_and_evict_per_owned_root(self):
        binding = self.open(max_bytes=5400)
        cache = binding.cache
        self.assertEqual(sum(value.cache.max_bytes for value in cache._bindings), 5400)
        for root in self.roots:
            for number in range(8):
                path = root / f'{number}.jpg'
                Image.new('RGB', (80, 60), (number * 20, 50, 90)).save(path, 'JPEG')
                cache.get_thumbnail(path)
        total = sum(binding.cache._connection.execute(
            'SELECT COALESCE(SUM(size),0) FROM thumbnails').fetchone()[0]
                    for binding in cache._bindings)
        self.assertLessEqual(total, 5400)
        cache.max_bytes = 9003
        self.assertEqual(sum(value.cache.max_bytes for value in cache._bindings), 9003)
        cache.close()
        cache.max_bytes = 12000
        self.assertEqual(cache.max_bytes, 12000)
        self.assertEqual(cache.writer_roots, ())

    def test_shared_global_cache_counts_budget_once_and_clear_keeps_all_choices(self):
        items = self.pairs()
        binding = self.open('user', max_bytes=10000)
        cache = binding.cache
        self.assertEqual(cache.actual_paths, (self.user / 'cache',))
        self.assertEqual(cache.writer_roots, ())
        self.assertTrue(all(value.cache.max_bytes == 10000 for value in cache._bindings))
        for item in items:
            cache.set_override(item, item.candidates[-1])
            cache.get_thumbnail(item.image_path)
        cache.clear_thumbnails()
        for item in items:
            self.assertEqual(cache.get_override(item), item.candidates[-1])
        cache.max_bytes = 12000
        self.assertTrue(all(value.cache.max_bytes == 12000 for value in cache._bindings))

    def test_portable_sources_migrate_to_global_without_cross_root_pixels_or_choices(self):
        items = self.pairs()
        # Same names and metadata are intentional; relative identities alone
        # must never import pixels or selections from a different selected root.
        common = items[0].image_path.stat()
        for item in items:
            os.utime(item.image_path, ns=(common.st_atime_ns, common.st_mtime_ns))
        portable = self.open()
        thumbnails = []
        for number, item in enumerate(items):
            thumbnails.append(portable.cache.get_thumbnail(item.image_path).read_bytes())
            portable.cache.set_override(item, item.candidates[number % 2])
        paths = portable.cache.actual_paths
        portable.close()
        global_binding = self.open('user', legacy_sources=paths)
        with patch('livegallery.cache.thumbnail_bytes', side_effect=AssertionError('valid lazy transfer should decode nothing')):
            for number, item in enumerate(items):
                self.assertEqual(global_binding.cache.get_thumbnail(item.image_path).read_bytes(), thumbnails[number])
                self.assertEqual(global_binding.cache.get_override(item), item.candidates[number % 2])

    def test_whole_parent_move_reuses_parent_and_nested_cache_without_decoding(self):
        items = self.pairs()
        binding = self.open()
        expected = []
        for item in items:
            expected.append(binding.cache.get_thumbnail(item.image_path).read_bytes())
            binding.cache.set_override(item, item.candidates[-1])
        binding.close()
        moved = self.base / 'moved-parent'
        self.parent.rename(moved)
        roots = (moved, moved / 'child', self.other)
        reopened = self.open(roots=roots)
        with patch('livegallery.cache.thumbnail_bytes', side_effect=AssertionError('move must reuse portable cache')):
            for number, root in enumerate(roots):
                item = next(item for item in scan_folder(root, recursive=False) if item.is_live)
                self.assertEqual(reopened.cache.get_thumbnail(item.image_path).read_bytes(), expected[number])
                self.assertEqual(reopened.cache.get_override(item), item.candidates[-1])

    def test_one_busy_library_falls_back_only_for_that_root_and_keeps_choice(self):
        items = self.pairs()
        owner = open_cache('library', self.child, user_base=self.user, program_base=self.program)
        self.bindings.append(owner)
        binding = self.open()
        self.assertTrue(binding.fallback)
        self.assertEqual(binding.cache.writer_roots, (self.parent, self.other))
        self.assertEqual(binding.cache.fallback_roots, (self.child,))
        self.assertNotEqual(binding.cache.base_for(items[1]), self.child / CACHE_DIRECTORY)
        binding.cache.set_override(items[1], items[1].candidates[-1])
        binding.close()
        owner.close()
        reopened = self.open()
        self.assertFalse(reopened.fallback)
        self.assertEqual(reopened.cache.get_override(items[1]), items[1].candidates[-1])

    def test_verify_groups_paths_aggregates_progress_and_cancels_between_roots(self):
        items = self.pairs()
        cache = self.open().cache
        paths = [item.image_path for item in items]
        updates = []
        result = cache.verify_files((*paths, paths[0]), progress=lambda done, total: updates.append((done, total)))
        self.assertEqual(result['checked'], 3)
        self.assertEqual(result['changed'], paths)
        self.assertEqual(updates, [(1, 3), (2, 3), (3, 3)])
        self.assertFalse(result['cancelled'])
        cancel = threading.Event()
        result = cache.verify_files(paths, cancel=cancel,
                                   progress=lambda done, total: cancel.set())
        self.assertEqual(result, {'checked': 1, 'changed': [], 'cancelled': True})

    def test_invalidate_and_forget_are_routed_without_changing_other_root_decisions(self):
        items = self.pairs()
        cache = self.open().cache
        for item in items:
            cache.put_decision(item, 'alg', PairDecision(item.image_path, (), False, item.id))
        cache.invalidate_item(items[1])
        self.assertIsNone(cache.get_decision(items[1], 'alg'))
        self.assertIsNotNone(cache.get_decision(items[0], 'alg'))
        self.assertIsNotNone(cache.get_decision(items[2], 'alg'))
        cache.forget_decision(items[2])
        self.assertIsNone(cache.get_decision(items[2], 'alg'))

    def test_close_waits_for_active_call_then_releases_every_root_lock(self):
        items = self.pairs()
        cache = self.open().cache
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = cache._store_for(items[0]).get_thumbnail
        def held(path, edge=256):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(path, edge)
        with patch.object(cache._store_for(items[0]), 'get_thumbnail', side_effect=held):
            worker = threading.Thread(target=lambda: cache.get_thumbnail(items[0].image_path))
            worker.start()
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=lambda: (cache.close(), closed.set()))
            closer.start()
            self.assertFalse(closed.wait(.1))
            with self.assertRaises(RuntimeError):
                cache.get_override(items[1])
            release.set()
            worker.join(3)
            closer.join(3)
        self.assertTrue(closed.is_set())
        self.assertTrue(all(binding.cache._closed for binding in cache._bindings))
        reopened = self.open()
        self.assertFalse(reopened.fallback)

    def test_partial_failure_or_cancel_releases_prepared_library_writers(self):
        real_open = open_cache
        calls = []
        def failed(mode, root, **kwargs):
            calls.append(root)
            if len(calls) == 2:
                raise OSError('second library unavailable')
            return real_open(mode, root, **kwargs)
        with patch('livegallery.multi_cache.open_cache', side_effect=failed):
            with self.assertRaises(OSError):
                self.open()
        fresh = self.open(roots=(self.parent,))
        self.assertFalse(fresh.fallback)
        fresh.close()
        cancel = threading.Event()
        def cancelled(mode, root, **kwargs):
            binding = real_open(mode, root, **kwargs)
            cancel.set()
            return binding
        with patch('livegallery.multi_cache.open_cache', side_effect=cancelled):
            with self.assertRaises(InterruptedError):
                self.open(cancel=cancel)
        fresh = self.open(roots=(self.parent,))
        self.assertFalse(fresh.fallback)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory attributes require Windows')
    def test_windows_readonly_directory_marker_does_not_force_library_fallback(self):
        import ctypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        api.GetFileAttributesW.restype = ctypes.c_uint32
        api.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        original = api.GetFileAttributesW(str(self.parent))
        self.assertNotEqual(original, 0xffffffff)
        self.assertTrue(api.SetFileAttributesW(str(self.parent), original | 0x1))
        try:
            binding = self.open()
            self.assertFalse(binding.fallback)
            self.assertEqual(binding.cache.writer_roots, self.roots)
            self.assertTrue((self.parent / CACHE_DIRECTORY / 'library.sqlite3').is_file())
        finally:
            api.SetFileAttributesW(str(self.parent), original)


if __name__ == '__main__':
    unittest.main()
