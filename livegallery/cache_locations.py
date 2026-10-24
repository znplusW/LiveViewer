"""Cache placement, managed-directory identity and process writer ownership."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import tempfile
import uuid

from .cache import CacheFormatError, CacheStore, _atomic_json

CACHE_DIRECTORY = 'LiveViewer Cache'
MANIFEST_NAME = 'manifest.json'
OWNERSHIP_SENTINEL = '.liveviewer-cache'
PRODUCT = 'LiveViewer.LibraryCache'
SCHEMA_VERSION = 1


def _linked(path):
    return path.is_symlink() or (getattr(path, 'is_junction', lambda: False)())


def read_manifest(path):
    path = Path(path)
    marker = path / MANIFEST_NAME
    if _linked(path) or _linked(marker) or not marker.is_file() or marker.stat().st_size > 16 * 1024:
        raise CacheFormatError('此目录不是可识别的 LiveViewer 缓存。')
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
    except (ValueError, OSError) as error:
        raise CacheFormatError('缓存标记无法读取，未修改该目录。') from error
    if not isinstance(value, dict) or value.get('product') != PRODUCT:
        raise CacheFormatError('同名目录不属于 LiveViewer，未修改其中的文件。')
    return value


def _has_sentinel(path):
    path = Path(path)
    marker = path / OWNERSHIP_SENTINEL
    try:
        return (not _linked(path) and not _linked(marker) and marker.is_file()
                and marker.stat().st_size <= 128
                and marker.read_bytes() == (PRODUCT + '\n').encode('ascii'))
    except OSError:
        return False


def _write_sentinel(path):
    marker = Path(path) / OWNERSHIP_SENTINEL
    if marker.exists() or marker.is_symlink():
        if not _has_sentinel(path):
            raise CacheFormatError('缓存身份文件无法识别，已保留该目录。')
        return
    descriptor, name = tempfile.mkstemp(prefix='identity-', suffix='.tmp', dir=path)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write((PRODUCT + '\n').encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)


def is_managed_cache_directory(path):
    """Recognize ownership for exclusion, independently of index compatibility.

    A surviving exact ownership sentinel keeps damaged manifest/cache JPEGs
    out of photo browsing. It never authorizes opening an unsupported cache.
    """
    if _has_sentinel(path):
        return True
    try:
        return read_manifest(path).get('product') == PRODUCT
    except (OSError, ValueError):
        return False


def _supported_manifest(path):
    value = read_manifest(path)
    if value.get('schema') != SCHEMA_VERSION or value.get('thumbnail_format', 3) != 3:
        raise CacheFormatError('图库缓存来自无法兼容的版本，已保留原目录。')
    try:
        uuid.UUID(value['library_id'])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise CacheFormatError('图库缓存身份标记无效，已保留原目录。') from error
    return value


def _new_manifest():
    return {'product': PRODUCT, 'schema': SCHEMA_VERSION,
            'library_id': str(uuid.uuid4()), 'thumbnail_format': 3}


def _hide_directory(path):
    if sys.platform != 'win32':
        return
    api = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
    api.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    api.GetFileAttributesW.restype = ctypes.c_uint32
    api.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    api.SetFileAttributesW.restype = ctypes.c_int
    flags = api.GetFileAttributesW(str(path))
    if flags == 0xffffffff or not api.SetFileAttributesW(str(path), flags | 0x2):
        raise OSError('无法把图库缓存设置为隐藏目录。')


class DirectoryWriterLock:
    """A nonblocking OS lock, released automatically after process failure."""
    def __init__(self, path):
        self.path = Path(path)
        if _linked(self.path):
            raise CacheFormatError('缓存锁文件是链接，未写入此目录。')
        self._stream = self.path.open('a+b')
        try:
            if self.path.stat().st_size == 0:
                self._stream.write(b'\0')
                self._stream.flush()
            self._stream.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._stream.close()
            self._stream = None
            raise

    def close(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.seek(0)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


@dataclass
class CacheBinding:
    cache: CacheStore
    warning: str = ''
    location: str = 'user'
    requested_location: str = 'user'

    @property
    def actual_path(self):
        return self.cache.base

    @property
    def fallback(self):
        return self.location != self.requested_location

    def close(self):
        self.cache.close()


def _writable(path):
    if not Path(path).stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError('照片目录为只读。')


def _fallback_parent(root, user_base):
    # UUID alone would mix two independently modified copies of a gallery.
    identity = str(Path(root).absolute())
    if sys.platform == 'win32':
        identity = identity.casefold()
    token = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return Path(user_base) / 'cache' / 'library-fallback' / token


def _fallback_sources(parent):
    if not parent.is_dir() or _linked(parent):
        return []
    result = []
    for child in parent.iterdir():
        try:
            _supported_manifest(child)
            if (child / 'library.sqlite3').is_file():
                result.append(child)
        except (OSError, ValueError):
            continue
    return result


def _unique_sources(sources):
    output, seen = [], set()
    for source in sources:
        key = ('object', id(source)) if isinstance(source, CacheStore) else ('path', str(Path(source).absolute()))
        if key not in seen:
            output.append(source)
            seen.add(key)
    return tuple(output)


def open_cache(location, library_root=None, *, user_base, program_base,
               max_bytes=2 * 1024 ** 3, legacy_sources=()):
    """Open a selected location off the GUI thread.

    The two original cache locations remain global and backward compatible.
    A library cache is relative and has exactly one writer. Busy, unwritable or
    unrecognized photo directories use a distinct local fallback; existing
    valid gallery entries may be read through without mutating the owner.
    """
    if location not in ('user', 'program', 'library'):
        raise ValueError('未知的缓存保存位置。')
    user_base, program_base = Path(user_base), Path(program_base)
    defaults = (user_base / 'cache', program_base / 'cache')
    if location != 'library' or library_root is None:
        actual = 'user' if location == 'library' else location
        target = defaults[0] if actual == 'user' else defaults[1]
        source_paths = _unique_sources((*legacy_sources, *(value for value in defaults if value != target)))
        try:
            if target.exists():
                _writable(target)
            else:
                parent = target.parent
                while not parent.exists():
                    parent = parent.parent
                _writable(parent)
            store = CacheStore(target, max_bytes, legacy_sources=source_paths, legacy_root=library_root)
            return CacheBinding(store, store.recovery_warning, actual, location)
        except (OSError, sqlite3.Error) as error:
            if actual != 'program':
                raise
            store = CacheStore(defaults[0], max_bytes, legacy_sources=source_paths, legacy_root=library_root)
            return CacheBinding(store, '软件目录不可写，已使用本机缓存。' + str(error), 'user', location)
    root = Path(library_root).absolute()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    candidate = root / CACHE_DIRECTORY
    # Case-insensitive naming is expected on Windows and NTFS. Respect the
    # existing spelling where the development filesystem is case-sensitive.
    matches = [child for child in root.iterdir() if child.name.casefold() == CACHE_DIRECTORY.casefold()]
    if len(matches) == 1:
        candidate = matches[0]
    fallback_parent = _fallback_parent(root, user_base)
    old_fallbacks = _fallback_sources(fallback_parent)
    sources = _unique_sources((*legacy_sources, *old_fallbacks, *defaults))
    readable_gallery = None
    origin_library_id = None
    lock = None
    try:
        if len(matches) > 1:
            raise CacheFormatError('存在多个同名缓存目录，未修改这些目录。')
        if candidate.exists():
            if not candidate.is_dir() or _linked(candidate):
                raise CacheFormatError('同名缓存位置不是普通文件夹，已保留原文件。')
            manifest = _supported_manifest(candidate)
            origin_library_id = manifest['library_id']
            readable_gallery = candidate
            _writable(candidate)
        else:
            _writable(root)
            candidate.mkdir()
            manifest = _new_manifest()
            _atomic_json(candidate / MANIFEST_NAME, manifest)
            origin_library_id = manifest['library_id']
            readable_gallery = candidate
        lock = DirectoryWriterLock(candidate / '.writer.lock')
        _write_sentinel(candidate)
        _hide_directory(candidate)
        store = CacheStore(candidate, max_bytes, library_root=root,
                           legacy_sources=sources, close_callback=lock.close,
                           library_id=origin_library_id)
        return CacheBinding(store, store.recovery_warning, 'library', 'library')
    except (OSError, ValueError, sqlite3.Error) as error:
        if lock is not None:
            lock.close()
        # Never clear, rename or repair an unrecognized same-named folder.
        # Each fallback has its own writer, but subsequent opens import its
        # timestamped choices so a busy instance cannot lose manual selections.
        warning = '图库缓存暂不可写，已使用本机缓存。' + str(error)
        fallback_parent.mkdir(parents=True, exist_ok=True)
        target = fallback_parent / uuid.uuid4().hex
        target.mkdir()
        marker = _new_manifest()
        marker.update(storage='fallback', root_scope=fallback_parent.name)
        if origin_library_id is not None:
            marker['origin_library_id'] = origin_library_id
        _atomic_json(target / MANIFEST_NAME, marker)
        fallback_lock = DirectoryWriterLock(target / '.writer.lock')
        try:
            _write_sentinel(target)
            extra = (readable_gallery,) if readable_gallery is not None else ()
            store = CacheStore(target, max_bytes, library_root=root,
                               legacy_sources=_unique_sources((*old_fallbacks, *extra, *sources)),
                               close_callback=fallback_lock.close,
                               library_id=origin_library_id)
        except BaseException:
            fallback_lock.close()
            raise
        return CacheBinding(store, warning + store.recovery_warning, 'user', 'library')

