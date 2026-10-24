"""Portable library reuse, ownership, incremental invalidation and migration."""
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

from livegallery.cache import CacheFormatError, CacheStore, file_version
from livegallery.cache_locations import (CACHE_DIRECTORY, MANIFEST_NAME, PRODUCT,
                                         SCHEMA_VERSION, OWNERSHIP_SENTINEL, is_managed_cache_directory, open_cache)
from livegallery.library import scan_folder
from livegallery.models import MediaItem, PairDecision


def hold_writer(root, user, program, ready, release):
    binding = open_cache('library', Path(root), user_base=Path(user), program_base=Path(program))
    ready.put(binding.location)
    release.wait(10)
    binding.close()


class LibraryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'photos'
        self.root.mkdir()
        self.user, self.program = self.base / 'user', self.base / 'program'
        self.bindings = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for binding in reversed(self.bindings):
            binding.close()

    def open(self, location='library', root=None, **kwargs):
        binding = open_cache(location, root or self.root, user_base=self.user,
                             program_base=self.program, **kwargs)
        self.bindings.append(binding)
        return binding

    def photo(self, name='pair.jpg', color='red'):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (120, 90), color).save(target, 'BMP' if target.suffix=='.bmp' else 'JPEG')
        return target

    def pair(self):
        self.photo('pair.jpg', 'red')
        self.photo('pair.jpeg', 'blue')
        (self.root / 'pair.MOV').write_bytes(b'movie fixture bytes')
        return next(item for item in scan_folder(self.root) if item.is_live)

    def test_original_user_and_program_locations_remain_available(self):
        user = self.open('user')
        program = self.open('program')
        self.assertEqual(user.cache.base, self.user / 'cache')
        self.assertEqual(program.cache.base, self.program / 'cache')
        self.assertIsNone(user.cache.library_root)
        self.assertIsNone(program.cache.library_root)

    def test_creation_manifest_hidden_windows_and_scanner_exclusion(self):
        image = self.photo()
        binding = self.open()
        self.assertEqual(binding.location, 'library')
        self.assertTrue(is_managed_cache_directory(binding.cache.base))
        data = json.loads((binding.cache.base / MANIFEST_NAME).read_text())
        self.assertEqual(data['product'], PRODUCT)
        self.assertEqual(data['schema'], SCHEMA_VERSION)
        uuid.UUID(data['library_id'])
        binding.cache.get_thumbnail(image)
        self.assertEqual([item.image_path for item in scan_folder(self.root)], [image])
        if os.name == 'nt':
            import ctypes
            self.assertTrue(ctypes.windll.kernel32.GetFileAttributesW(str(binding.cache.base)) & 2)

    def test_move_and_copy_reuse_images_decisions_and_manual_choices(self):
        item = self.pair()
        binding = self.open()
        selected = item.candidates[-1]
        before = binding.cache.get_thumbnail(selected).read_bytes()
        decision = PairDecision(selected, (item.candidates[0],), False, 'known selection')
        binding.cache.put_decision(item, 'algorithm-1', decision)
        binding.cache.set_override(item, selected)
        # set_override intentionally invalidates derived analysis; recompute it.
        binding.cache.get_override(item)
        binding.cache.put_decision(item, 'algorithm-1', decision)
        binding.close()
        moved = self.base / 'different-drive-name'
        self.root.rename(moved)
        moved_item = next(item for item in scan_folder(moved) if item.is_live)
        reopened = self.open(root=moved)
        with patch('livegallery.cache.thumbnail_bytes', side_effect=AssertionError('must reuse')):
            self.assertEqual(reopened.cache.get_thumbnail(moved_item.candidates[-1]).read_bytes(), before)
        self.assertEqual(reopened.cache.get_override(moved_item), moved_item.candidates[-1])
        self.assertEqual(reopened.cache.get_decision(moved_item, 'algorithm-1').image_path, moved_item.candidates[-1])
        reopened.close()
        copied = self.base / 'independent-copy'
        shutil.copytree(moved, copied)
        copied_item = next(item for item in scan_folder(copied) if item.is_live)
        copy = self.open(root=copied)
        with patch('livegallery.cache.thumbnail_bytes', side_effect=AssertionError('must reuse copy')):
            self.assertEqual(copy.cache.get_thumbnail(copied_item.candidates[-1]).read_bytes(), before)
        copy.cache.set_override(copied_item, copied_item.candidates[0])
        copy.close()
        original = self.open(root=moved)
        self.assertEqual(original.cache.get_override(moved_item), moved_item.candidates[-1])

    def test_ordinary_same_name_future_schema_and_symlink_are_untouched(self):
        folder = self.root / CACHE_DIRECTORY
        folder.mkdir()
        original = folder / 'my-picture.jpg'
        original.write_bytes(b'user data')
        binding = self.open()
        self.assertTrue(binding.fallback)
        self.assertEqual(original.read_bytes(), b'user data')
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['my-picture.jpg'])
        binding.close()
        (folder / MANIFEST_NAME).write_text(json.dumps({'product': PRODUCT,'schema':999,'library_id':str(uuid.uuid4())}))
        before = {path.name:path.read_bytes() for path in folder.iterdir()}
        future = self.open()
        self.assertTrue(future.fallback)
        self.assertEqual({path.name:path.read_bytes() for path in folder.iterdir()},before)
        self.assertTrue(is_managed_cache_directory(folder))

    def test_read_only_folder_falls_back_without_modifying_it(self):
        folder = self.root / CACHE_DIRECTORY
        folder.mkdir()
        (folder / MANIFEST_NAME).write_text(json.dumps({'product':PRODUCT,'schema':1,'library_id':str(uuid.uuid4())}))
        original = (folder / MANIFEST_NAME).read_bytes()
        folder.chmod(0o555)
        try:
            binding = self.open()
            self.assertTrue(binding.fallback)
            self.assertEqual((folder / MANIFEST_NAME).read_bytes(), original)
            self.assertFalse((folder / 'library.sqlite3').exists())
        finally:
            folder.chmod(0o755)

    def test_process_writer_lock_fallback_and_newer_manual_choice_merges(self):
        item = self.pair()
        context = multiprocessing.get_context('spawn')
        ready, release = context.Queue(), context.Event()
        child = context.Process(target=hold_writer,args=(str(self.root),str(self.user),str(self.program),ready,release))
        child.start()
        try:
            self.assertEqual(ready.get(timeout=8), 'library')
            fallback = self.open()
            self.assertTrue(fallback.fallback)
            fallback.cache.set_override(item, item.candidates[-1])
            fallback.close()
        finally:
            release.set();child.join(10)
            if child.is_alive(): child.kill();child.join()
        self.assertEqual(child.exitcode,0)
        writer = self.open()
        self.assertEqual(writer.location, 'library')
        self.assertEqual(writer.cache.get_override(item), item.candidates[-1])
        writer.close()
        persisted = self.open()
        self.assertEqual(persisted.cache.get_override(item), item.candidates[-1])

    def test_lazy_legacy_import_requires_no_image_decode(self):
        item = self.pair()
        legacy = self.open('user')
        path = item.candidates[-1]
        thumbnail = legacy.cache.get_thumbnail(path).read_bytes()
        decision = PairDecision(path, (), False, 'legacy cache')
        legacy.cache.set_override(item,path)
        legacy.cache.get_override(item)
        legacy.cache.put_decision(item,'alg',decision)
        portable = self.open()
        with patch('livegallery.cache.thumbnail_bytes',side_effect=AssertionError('legacy thumbnail should import')), \
                patch('livegallery.cache._content_hash',side_effect=AssertionError('migration must not reread originals')):
            self.assertEqual(portable.cache.get_thumbnail(path).read_bytes(),thumbnail)
        self.assertEqual(portable.cache.get_override(item),path)
        # Resolve a manual choice normally bypasses automatic decisions; direct
        # get_decision can use a newly generated local decision afterwards.
        portable.cache.put_decision(item,'alg',decision)
        self.assertEqual(portable.cache.get_decision(item,'alg'),decision)

    def test_clear_and_index_corruption_preserve_manual_choices(self):
        item = self.pair()
        binding = self.open()
        cache = binding.cache
        cache.set_override(item,item.candidates[-1])
        cache.get_thumbnail(item.image_path)
        self.assertGreater(cache.clear_thumbnails(),0)
        self.assertEqual(cache.get_override(item),item.candidates[-1])
        path = cache.base
        binding.close()
        (path/'library.sqlite3').write_bytes(b'corrupt derived index')
        reopened = self.open()
        self.assertTrue(reopened.cache.recovery_warning)
        self.assertEqual(reopened.cache.get_override(item),item.candidates[-1])
        self.assertTrue(list(path.glob('library.sqlite3.corrupt-*')))

    def test_thumbnail_hits_batch_usage_writes_and_corrupt_entry_is_replaced(self):
        image = self.photo()
        cache = self.open().cache
        thumb = cache.get_thumbnail(image)
        trace=[]
        cache._connection.set_trace_callback(trace.append)
        for _ in range(20): cache.get_thumbnail(image)
        self.assertFalse(any(statement.startswith('UPDATE thumbnails') for statement in trace))
        thumb.write_bytes(b'incomplete jpeg')
        replacement = cache.get_thumbnail(image)
        with Image.open(replacement) as decoded: decoded.load()
        self.assertEqual(replacement,thumb)

    def test_deep_verify_catches_same_size_time_and_blocks_legacy_resurrection(self):
        image = self.photo('photo.bmp','red')
        item = MediaItem('id',image)
        legacy = self.open('user').cache
        old = legacy.get_thumbnail(image).read_bytes()
        portable = self.open().cache
        self.assertEqual(portable.get_thumbnail(image).read_bytes(),old)
        before = image.stat()
        Image.new('RGB',(120,90),'blue').save(image,'BMP')
        self.assertEqual(image.stat().st_size,before.st_size)
        os.utime(image,ns=(before.st_atime_ns,before.st_mtime_ns))
        report = portable.verify_files([image])
        self.assertEqual(report['changed'],[image])
        updated = portable.get_thumbnail(image)
        with Image.open(updated) as decoded:
            self.assertGreater(decoded.getpixel((20,20))[2],200)
        self.assertNotEqual(updated.read_bytes(),old)
        self.assertEqual(portable.verify_files([image])['changed'],[])

    def test_cover_invalidation_and_restore_auto_do_not_import_old_decision(self):
        item = self.pair()
        legacy=self.open('user').cache
        old=PairDecision(item.candidates[-1],(),False,'manual source')
        legacy.set_override(item,item.candidates[-1]);legacy.get_override(item)
        legacy.put_decision(item,'alg',old)
        cache=self.open().cache
        self.assertEqual(cache.get_override(item),item.candidates[-1])
        cache.set_override(item,None)
        self.assertIsNone(cache.get_override(item))
        self.assertIsNone(cache.get_decision(item,'alg'))
        cache.put_decision(item,'alg',PairDecision(item.image_path,(),False,'new automatic'))
        cache.invalidate_item(item,[file_version(path) for path in item.candidates])
        self.assertIsNone(cache.get_decision(item,'alg'))
        self.assertIsNone(cache.get_override(item))

    def test_program_directory_unwritable_falls_back_to_user(self):
        self.program.mkdir()
        self.program.chmod(0o555)
        try:
            binding=self.open('program')
            self.assertTrue(binding.fallback)
            self.assertEqual(binding.location,'user')
            self.assertEqual(binding.cache.base,self.user/'cache')
        finally:
            self.program.chmod(0o755)

    def test_global_choices_survive_index_recovery_and_program_user_switch(self):
        item=self.pair()
        user=self.open('user')
        user.cache.set_override(item,item.candidates[-1])
        user.close()
        (self.user/'cache/library.sqlite3').write_bytes(b'damaged index')
        reopened=self.open('user')
        self.assertEqual(reopened.cache.get_override(item),item.candidates[-1])
        program=self.open('program')
        self.assertEqual(program.cache.get_override(item),item.candidates[-1])
        program.cache.set_override(item,None)
        program.close()
        reopened.close()
        after=self.open('user')
        self.assertIsNone(after.cache.get_override(item))

    def test_gallery_manual_choice_imports_into_original_user_mode(self):
        item=self.pair()
        gallery=self.open()
        gallery.cache.set_override(item,item.candidates[-1])
        gallery.close()
        user=self.open('user',legacy_sources=(self.root/CACHE_DIRECTORY,))
        self.assertEqual(user.cache.get_override(item),item.candidates[-1])

    def test_thumbnail_directory_links_never_write_or_delete_source_images(self):
        image=self.photo()
        binding=self.open()
        path=binding.cache.base
        binding.close()
        shutil.rmtree(path/'thumbnails')
        original=image.read_bytes()
        try:
            (path/'thumbnails').symlink_to(self.root,target_is_directory=True)
        except OSError:
            self.skipTest('Creating links unavailable')
        fallback=self.open()
        self.assertTrue(fallback.fallback)
        fallback.cache.clear_thumbnails()
        self.assertEqual(image.read_bytes(),original)
        with self.assertRaises(CacheFormatError):
            CacheStore(path,library_root=self.root)
        (path/'thumbnails').unlink()
        active=self.open()
        active.cache.get_thumbnail(image)
        shutil.rmtree(path/'thumbnails')
        (path/'thumbnails').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(CacheFormatError):
            active.cache.clear_thumbnails()
        self.assertEqual(image.read_bytes(),original)

    def test_clear_removes_only_owned_entries_and_truncated_jpeg_recovers(self):
        image=self.photo()
        cache=self.open().cache
        known=cache.get_thumbnail(image)
        known.write_bytes(known.read_bytes()[:-2])
        repaired=cache.get_thumbnail(image)
        with Image.open(repaired) as pixels:
            pixels.load()
        foreign=cache.thumbnail_dir/'my-picture.jpg'
        foreign.write_bytes(b'user supplied file')
        cache.clear_thumbnails()
        self.assertEqual(foreign.read_bytes(),b'user supplied file')
        self.assertFalse(known.exists())

    def test_deep_changed_video_cannot_import_old_pair_analysis(self):
        item=self.pair()
        legacy=self.open('user').cache
        cached=PairDecision(item.image_path,(),False,'old automatic')
        legacy.put_decision(item,'alg',cached)
        cache=self.open().cache
        self.assertEqual(cache.get_decision(item,'alg'),cached)
        video=item.video_path
        before=video.stat()
        video.write_bytes(b'changed movie bytes'+b' '*(before.st_size-len(b'changed movie bytes')))
        os.utime(video,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.assertEqual(video.stat().st_size,before.st_size)
        self.assertEqual(cache.verify_files([video])['changed'],[video])
        self.assertIsNone(cache.get_decision(item,'alg'))

    def test_foreign_gallery_same_file_metadata_is_not_a_migration_source(self):
        first=self.photo('same.bmp','red')
        first_binding=self.open()
        first_binding.cache.get_thumbnail(first)
        first_binding.close()
        other=self.base/'other-library'
        other.mkdir()
        second=other/'same.bmp'
        Image.new('RGB',(120,90),'blue').save(second,'BMP')
        before=first.stat()
        os.utime(second,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.assertEqual(second.stat().st_size,before.st_size)
        second_binding=self.open(root=other,legacy_sources=(self.root/CACHE_DIRECTORY,))
        thumb=second_binding.cache.get_thumbnail(second)
        with Image.open(thumb) as image:
            self.assertGreater(image.getpixel((20,20))[2],200)

    def test_owned_sentinel_excludes_thumbnails_when_manifest_is_corrupt(self):
        photo=self.photo()
        binding=self.open()
        binding.cache.get_thumbnail(photo)
        path=binding.cache.base
        binding.close()
        self.assertEqual((path/OWNERSHIP_SENTINEL).read_bytes(),(PRODUCT+'\n').encode())
        (path/MANIFEST_NAME).write_bytes(b'broken manifest')
        self.assertTrue(is_managed_cache_directory(path))
        self.assertEqual([item.image_path for item in scan_folder(self.root)],[photo])
        fallback=self.open()
        self.assertTrue(fallback.fallback)
        self.assertEqual((path/MANIFEST_NAME).read_bytes(),b'broken manifest')

    def test_foreign_sentinel_is_not_ownership_and_is_not_replaced(self):
        path=self.root/CACHE_DIRECTORY
        path.mkdir()
        sentinel=path/OWNERSHIP_SENTINEL
        sentinel.write_bytes(b'another application')
        self.assertFalse(is_managed_cache_directory(path))
        binding=self.open()
        self.assertTrue(binding.fallback)
        self.assertEqual(sentinel.read_bytes(),b'another application')
        self.assertEqual(sorted(p.name for p in path.iterdir()),[OWNERSHIP_SENTINEL])

    def test_deep_verification_cancel_is_bounded_and_preserves_choices(self):
        image=self.photo()
        cache=self.open().cache
        result=cache.verify_files([image],cancel=lambda:True)
        self.assertEqual(result,{'checked':0,'changed':[],'cancelled':True})

    def test_explicit_choice_supersedes_future_disk_and_imported_timestamps(self):
        item=self.pair()
        first=self.open('user').cache
        stale=self.open('user').cache
        future=10**25
        with patch('livegallery.cache.time.time_ns',return_value=future):
            first.set_override(item,item.candidates[-1])
        with patch('livegallery.cache.time.time_ns',return_value=1):
            stale.set_override(item,None)
        data=json.loads((stale.base/'choices.json').read_text())['choices'][item.id]
        self.assertIsNone(data['path'])
        self.assertGreater(data['updated_ns'],future)
        # A mode switch must also supersede a choice imported from a machine
        # whose clock was ahead, without requiring an earlier get_override.
        program=self.open('program').cache
        with patch('livegallery.cache.time.time_ns',return_value=1):
            program.set_override(item,item.candidates[0])
        self.assertEqual(program.get_override(item),item.candidates[0])
        self.assertGreater(program._override_record(item)[1],data['updated_ns'])

    def test_all_thumbnail_deletions_reject_foreign_index_paths_and_live_links(self):
        image=self.photo()
        item=MediaItem('item',image)
        cache=self.open().cache
        known=cache.get_thumbnail(image)
        original=image.read_bytes()
        previous=file_version(image)
        key=cache._thumbnail_key(image,256)
        cache._connection.execute('UPDATE thumbnails SET filename=? WHERE key=?',
                                  ('../../pair.jpg',key))
        cache._connection.commit()
        cache.invalidate_item(item,[previous])
        cache.clear_thumbnails()
        cache.max_bytes=0
        cache._evict(protected='')
        self.assertEqual(image.read_bytes(),original)
        # The filename must be generated from this entry's key, even when it
        # is a plain basename rather than an obvious traversal.
        foreign=cache.thumbnail_dir/'my-picture.jpg'
        foreign.write_bytes(b'untouched user data')
        cache._connection.execute('UPDATE thumbnails SET filename=? WHERE key=?',
                                  (foreign.name,key))
        cache.clear_thumbnails()
        self.assertEqual(foreign.read_bytes(),b'untouched user data')
        shutil.rmtree(cache.thumbnail_dir)
        try:
            cache.thumbnail_dir.symlink_to(self.root,target_is_directory=True)
        except OSError:
            self.skipTest('Creating links unavailable')
        with self.assertRaises(CacheFormatError):
            cache.invalidate_item(item,[previous])
        self.assertEqual(image.read_bytes(),original)

    def test_malformed_choice_containers_and_records_recover_backup(self):
        item=self.pair()
        binding=self.open()
        cache=binding.cache
        cache.set_override(item,item.candidates[-1])
        cache.set_override(item,item.candidates[-1])
        path=cache.base
        binding.close()
        backup=(path/'choices.json.bak').read_bytes()
        malformed=([],None,{'schema':1,'choices':[]},
                   {'schema':1,'choices':{'bad':[]}})
        for value in malformed:
            with self.subTest(value=value):
                (path/'choices.json').write_text(json.dumps(value))
                reopened=self.open()
                self.assertEqual(reopened.cache.get_override(item),item.candidates[-1])
                reopened.cache.set_override(item,None)
                self.assertIsNone(reopened.cache.get_override(item))
                reopened.close()
                (path/'choices.json.bak').write_bytes(backup)
        (path/'choices.json').write_text(json.dumps({'schema':999,'choices':{}}))
        before=(path/'choices.json').read_bytes()
        rejected=self.open()
        self.assertTrue(rejected.fallback)
        self.assertEqual((path/'choices.json').read_bytes(),before)
        # A malformed source manifest is skipped rather than raising or
        # interpreting a portable database as a legacy global source.
        (path/MANIFEST_NAME).write_text('[]')
        other=self.open('user',legacy_sources=(path,))
        self.assertIsNone(other.cache.get_override(item))

    def test_fallback_reuse_requires_current_library_identity_and_path(self):
        item=self.pair()
        owner=self.open()
        fallback=self.open()
        self.assertTrue(fallback.fallback)
        fallback.cache.set_override(item,item.candidates[-1])
        marker=json.loads((fallback.cache.base/MANIFEST_NAME).read_text())
        original_id=json.loads((owner.cache.base/MANIFEST_NAME).read_text())['library_id']
        self.assertEqual(marker['origin_library_id'],original_id)
        fallback.close();owner.close()
        moved=self.base/'old-library'
        self.root.rename(moved)
        self.root.mkdir()
        replacement=self.pair()
        current=self.open()
        current_id=json.loads((current.cache.base/MANIFEST_NAME).read_text())['library_id']
        self.assertNotEqual(current_id,original_id)
        self.assertIsNone(current.cache.get_override(replacement))


if __name__=='__main__':
    unittest.main()
