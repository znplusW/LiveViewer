"""Persistent derived cache with portable library identities and durable choices."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid

from PIL import Image

from .imaging import thumbnail_bytes
from .models import MediaItem, PairDecision


class CacheFormatError(OSError):
    """An unsupported cache must not be modified by this program."""


def file_version(path: Path) -> tuple:
    path = Path(path)
    try:
        value = path.stat()
        return str(path.absolute()), value.st_size, value.st_mtime_ns
    except OSError:
        return str(path.absolute()), None, None


def item_version(item: MediaItem, algorithm: str) -> str:
    values = [file_version(path) for path in (item.candidates or (item.image_path,))]
    if item.video_path:
        values.append(file_version(item.video_path))
    return hashlib.sha256(json.dumps([algorithm, values]).encode()).hexdigest()


def _atomic_json(path, data):
    descriptor, name = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _content_hash(path, cancel=None):
    digest = hashlib.sha256()
    before = Path(path).stat()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            if cancel and cancel():
                raise InterruptedError('已取消深入校验。')
            digest.update(chunk)
    after = Path(path).stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise OSError('文件在校验期间发生变化，请稍后重试。')
    return digest.hexdigest()


class CacheStore:
    """Legacy global cache or a library-root-relative cache.

    ``legacy_sources`` may contain read-only CacheStore objects or directories.
    Directory readers are owned and closed by this store. A portable cache
    keeps manual choices in an atomic, backed-up JSON file outside its derived
    SQLite index. Its caller owns the directory writer lock.
    """
    def __init__(self, base: Path, max_bytes: int = 2 * 1024 ** 3, *,
                 library_root: Path | None = None, legacy_sources=(),
                 read_only=False, close_callback=None, legacy_root=None,
                 library_id=None):
        self.base = Path(base)
        self.library_root = Path(library_root).absolute() if library_root is not None else None
        self._library_id = library_id
        self.read_only = bool(read_only)
        self._legacy_root = self.library_root or (Path(legacy_root).absolute() if legacy_root is not None else None)
        self.max_bytes = max(0, int(max_bytes))
        self._lock = threading.RLock()
        self._closed = False
        self._close_callback = close_callback
        self._decision_revisions = {}
        self._requests = threading.local()
        self._legacy_specs = tuple(legacy_sources)
        self._legacy_readers = []
        self._legacy_loaded = False
        self._owned_readers = []
        self._touches = {}
        self._last_touch_flush = time.monotonic()
        self._choices = {}
        self._choices_primary_valid = True
        self.recovery_warning = ''
        self._connection = None
        if not self.read_only:
            self.base.mkdir(parents=True, exist_ok=True)
        self.thumbnail_dir = self.base / 'thumbnails'
        if self.thumbnail_dir.is_symlink() or getattr(self.thumbnail_dir, 'is_junction', lambda: False)():
            raise CacheFormatError('缩略图缓存位置是链接，未写入此目录。')
        if not self.read_only:
            self.thumbnail_dir.mkdir(exist_ok=True)
        self._load_choices()
        try:
            self._open_index()
        except sqlite3.DatabaseError:
            if self._connection is not None:
                self._connection.close()
            if self.read_only:
                raise
            # Keep damaged bytes for recovery; never discard durable choices.
            suffix = '.corrupt-' + uuid.uuid4().hex
            for name in ('library.sqlite3', 'library.sqlite3-wal', 'library.sqlite3-shm'):
                path = self.base / name
                if path.exists():
                    path.replace(path.with_name(path.name + suffix))
            self.recovery_warning = '缓存索引损坏，已保留原索引并重新建立；手动选择仍保留。'
            self._open_index()
        if not self.read_only:
            self._evict(protected='')
            self._connection.commit()

    def _open_index(self):
        path = self.base / 'library.sqlite3'
        if path.is_symlink():
            raise CacheFormatError('缓存索引是符号链接，未写入此目录。')
        if self.read_only:
            self._connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                               timeout=2, check_same_thread=False)
        else:
            self._connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._connection.execute('PRAGMA busy_timeout=10000')
        version = self._connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self._connection.close()
            self._connection = None
            raise CacheFormatError('缓存数据库来自更新版本，已保留原文件。')
        if self.read_only:
            return
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.executescript('''
            CREATE TABLE IF NOT EXISTS thumbnails (
                key TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL, used REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (
                item_id TEXT PRIMARY KEY, version TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS overrides (item_id TEXT PRIMARY KEY, path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS file_records (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, digest TEXT, revision INTEGER NOT NULL DEFAULT 0);
            PRAGMA user_version=1;
        ''')

    def _check_open(self):
        if self._closed:
            raise RuntimeError('缓存已关闭。')

    def _safe_thumbnails(self):
        # Recheck before destructive operations too: a directory can be
        # replaced by a link while the application is already running.
        if self.thumbnail_dir.is_symlink() or getattr(self.thumbnail_dir, 'is_junction', lambda: False)():
            raise CacheFormatError('缩略图目录已变成链接，已停止写入或清理。')
        try:
            self.thumbnail_dir.resolve().relative_to(self.base.resolve())
        except ValueError as error:
            raise CacheFormatError('缩略图目录超出缓存范围。') from error

    def _unlink_thumbnail(self, key, filename):
        """Remove only a generated filename inside the current real directory."""
        self._safe_thumbnails()
        if (not isinstance(key, str) or len(key) != 64
                or any(character not in '0123456789abcdef' for character in key)
                or filename != key + '.jpg'):
            return None
        path = self.thumbnail_dir / filename
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            return None
        try:
            size = path.stat().st_size if path.exists() else 0
            path.unlink(missing_ok=True)
            return size
        except OSError:
            return None

    def _path(self, path):
        path = Path(path).absolute()
        if self.library_root is None:
            return str(path)
        try:
            return path.relative_to(self.library_root).as_posix()
        except ValueError as error:
            raise ValueError('文件不属于当前照片根目录。') from error

    def _decode_path(self, value):
        if not isinstance(value, str):
            raise ValueError('无效的缓存路径。')
        if self.library_root is None:
            return Path(value)
        # Reject Windows absolute paths even during development on macOS.
        import ntpath
        relative = PurePosixPath(value.replace('\\', '/'))
        if relative.is_absolute() or ntpath.isabs(value) or '..' in relative.parts:
            raise ValueError('缓存路径超出照片根目录。')
        return self.library_root.joinpath(*relative.parts)

    def _item_key(self, item):
        if self.library_root is None:
            return item.id
        source = item.video_path if item.video_path else item.image_path
        relative = self._path(source)
        kind = 'live' if item.video_path else 'image'
        # Different image candidates remain one live pair, independent of the
        # currently preferred cover or a drive-letter change.
        if item.video_path:
            relative = str(PurePosixPath(relative).with_suffix(''))
        return hashlib.sha256((kind + '\0' + relative.casefold()).encode()).hexdigest()[:24]

    def _version(self, path):
        absolute, size, modified = file_version(path)
        value = (self._path(path), size, modified)
        try:
            row = self._connection.execute('SELECT revision FROM file_records WHERE path=?', (value[0],)).fetchone()
        except sqlite3.OperationalError:
            row = None  # A pre-v0.7 read-only source has no fingerprint table.
        return value + (row[0],) if row and row[0] else value

    def _item_version(self, item, algorithm):
        values = [self._version(path) for path in (item.candidates or (item.image_path,))]
        if item.video_path:
            values.append(self._version(item.video_path))
        return hashlib.sha256(json.dumps([algorithm, values]).encode()).hexdigest()

    def _thumbnail_key(self, path, edge, version=None):
        version = self._version(path) if version is None else version
        family = 'thumbnail-v3-relative' if self.library_root is not None else 'thumbnail-v2'
        return hashlib.sha256(json.dumps([family, edge, version]).encode()).hexdigest()

    def _record_file(self, path):
        """Hash only files already being decoded/analyzed, never on cache hit."""
        if self.read_only:
            return
        name, size, modified = file_version(path)
        if size is None:
            return
        relative = self._path(path)
        with self._lock:
            row = self._connection.execute('SELECT size,mtime_ns,digest FROM file_records WHERE path=?', (relative,)).fetchone()
        if row and row[:2] == (size, modified) and row[2]:
            return
        # Live Photo clips are short. A very large non-Live MOV is left for
        # explicitly requested deep verification instead of adding a huge read.
        digest = _content_hash(path) if size <= 64 * 1024 ** 2 else None
        if file_version(path)[1:] != (size, modified):
            return
        with self._lock:
            self._connection.execute('''INSERT INTO file_records(path,size,mtime_ns,digest,revision)
                VALUES (?,?,?,?,0) ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,mtime_ns=excluded.mtime_ns,digest=excluded.digest''',
                (relative, size, modified, digest))

    def _sources(self):
        with self._lock:
            if not self._legacy_loaded:
                self._legacy_loaded = True
                for source in self._legacy_specs:
                    if isinstance(source, CacheStore):
                        if (source is not self and (source.library_root is None
                                or source.library_root == self._legacy_root)):
                            self._legacy_readers.append(source)
                        continue
                    path = Path(source)
                    if path.absolute() == self.base.absolute() or not (path / 'library.sqlite3').is_file():
                        continue
                    try:
                        portable = False
                        if (path / 'manifest.json').is_file():
                            marker = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                            if not isinstance(marker, dict):
                                continue
                            portable = marker.get('product') == 'LiveViewer.LibraryCache'
                        if portable:
                            if self._legacy_root is None:
                                continue
                            if marker.get('storage') == 'fallback':
                                identity = str(self._legacy_root.absolute())
                                if os.name == 'nt':
                                    identity = identity.casefold()
                                scope = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
                                if marker.get('root_scope') != scope:
                                    continue
                                if (self._library_id is not None
                                        and marker.get('origin_library_id') != self._library_id):
                                    continue
                            elif (path.name.casefold() != 'liveviewer cache'
                                  or path.parent.absolute() != self._legacy_root):
                                # A same-named file in another library is not
                                # a migration source for this library.
                                continue
                        reader = CacheStore(path, self.max_bytes,
                                            library_root=self._legacy_root if portable else None,
                                            read_only=True,
                                            library_id=(marker.get('origin_library_id')
                                                if portable and marker.get('storage') == 'fallback'
                                                else marker.get('library_id') if portable else None))
                    except (OSError, sqlite3.Error, ValueError):
                        continue
                    self._legacy_readers.append(reader)
                    self._owned_readers.append(reader)
            return tuple(self._legacy_readers)

    def _import_fingerprint(self, source, path):
        """Reuse a stored baseline during migration without rereading media."""
        try:
            with source._lock:
                row = source._connection.execute(
                    'SELECT size,mtime_ns,digest FROM file_records WHERE path=?',
                    (source._path(path),)).fetchone()
            if row is None or row[:2] != file_version(path)[1:]:
                return
            with self._lock:
                self._connection.execute('''INSERT INTO file_records(path,size,mtime_ns,digest,revision)
                    VALUES (?,?,?,?,0) ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,mtime_ns=excluded.mtime_ns,digest=excluded.digest''',
                    (self._path(path), *row))
        except (sqlite3.Error, OSError, ValueError):
            pass

    @staticmethod
    def _valid_thumbnail(path, edge=4096):
        try:
            if path.is_symlink():
                return False
            with Image.open(path) as image:
                if (image.format != 'JPEG' or min(image.size) < 1
                        or max(image.size) > min(4096, max(1, int(edge)))):
                    return False
                # verify() checks JPEG headers but can accept a truncated scan.
                # Thumbnail-sized full decode catches corrupt payloads too.
                image.load()
            return True
        except (OSError, ValueError, SyntaxError):
            return False

    def _existing_thumbnail(self, path, edge):
        with self._lock:
            self._check_open()
            self._safe_thumbnails()
            key = self._thumbnail_key(path, edge)
            target = self.thumbnail_dir / (key + '.jpg')
            return target if target.is_file() and self._valid_thumbnail(target, edge) else None

    def _touch(self, key):
        if self.read_only:
            return
        self._touches[key] = time.time()
        if len(self._touches) >= 128 or time.monotonic() - self._last_touch_flush >= 5:
            self._flush_touches()

    def _flush_touches(self):
        if self.read_only or not self._touches:
            return
        self._connection.executemany('UPDATE thumbnails SET used=? WHERE key=?',
                                     [(used, key) for key, used in self._touches.items()])
        self._touches.clear()
        self._last_touch_flush = time.monotonic()
        self._connection.commit()

    def get_thumbnail(self, path: Path, edge: int = 256) -> Path:
        with self._lock:
            self._check_open()
            self._safe_thumbnails()
            version = self._version(path)
            key = self._thumbnail_key(path, edge, version)
            target = self.thumbnail_dir / (key + '.jpg')
            if target.is_file() and self._valid_thumbnail(target, edge):
                if not self.read_only:
                    known = self._connection.execute('SELECT 1 FROM thumbnails WHERE key=?', (key,)).fetchone()
                    if known is None:
                        self._connection.execute('INSERT INTO thumbnails VALUES (?,?,?,?)',
                                                 (key, target.name, target.stat().st_size, time.time()))
                        self._evict(protected=key)
                        self._connection.commit()
                self._touch(key)
                return target
        if self.read_only:
            raise OSError('此缓存当前为只读，缺失缩略图需要使用本机缓存。')
        content = None
        imported_source = None
        # A content-generation bump deliberately supersedes identical old
        # size/time metadata; importing legacy pixels would resurrect them.
        sources = self._sources() if len(version) == 3 else ()
        for source in sources:
            try:
                old = source._existing_thumbnail(path, edge)
                if old is not None:
                    content = old.read_bytes()
                    imported_source = source
                    break
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                continue
        if content is None:
            content = thumbnail_bytes(path, edge)
        if len(content) > self.max_bytes:
            raise ValueError('The thumbnail exceeds the configured cache size limit')
        self._safe_thumbnails()
        descriptor, name = tempfile.mkstemp(prefix='thumb-', suffix='.tmp', dir=self.thumbnail_dir)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
            with self._lock:
                self._check_open()
                self._safe_thumbnails()
                # Never publish pixels decoded from a version which changed
                # while the job was running.
                if self._version(path) != version:
                    raise OSError('图片读取期间发生变化，请重试。')
                if not target.is_file() or not self._valid_thumbnail(target, edge):
                    temporary.replace(target)
                self._connection.execute('INSERT OR REPLACE INTO thumbnails VALUES (?,?,?,?)',
                                         (key, target.name, len(content), time.time()))
                self._evict(protected=key)
                self._connection.commit()
            if imported_source is None:
                self._record_file(path)
            else:
                self._import_fingerprint(imported_source, path)
            with self._lock:
                self._connection.commit()
            return target
        finally:
            try:
                self._safe_thumbnails()
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _evict(self, protected):
        self._safe_thumbnails()
        self._flush_touches()
        total = self._connection.execute('SELECT COALESCE(SUM(size),0) FROM thumbnails').fetchone()[0]
        if total <= self.max_bytes:
            return
        for key, filename, size in self._connection.execute(
                'SELECT key,filename,size FROM thumbnails WHERE key<>? ORDER BY used', (protected,)).fetchall():
            if self._unlink_thumbnail(key, filename) is None:
                continue
            self._connection.execute('DELETE FROM thumbnails WHERE key=?', (key,))
            total -= size
            if total <= self.max_bytes:
                break

    @staticmethod
    def _read_choices(path):
        if path.is_symlink() or path.stat().st_size > 8 * 1024 ** 2:
            raise ValueError('Invalid choices file')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or 'schema' not in data:
            raise ValueError('Invalid choices container')
        if data['schema'] != 1:
            raise CacheFormatError('手动选择记录来自无法识别的版本，已保留原文件。')
        records = data.get('choices')
        if not isinstance(records, dict):
            raise ValueError('Invalid choices records')
        for key, value in records.items():
            if (not isinstance(key, str) or not isinstance(value, dict)
                    or 'path' not in value
                    or (value['path'] is not None and not isinstance(value['path'], str))
                    or type(value.get('updated_ns')) is not int or value['updated_ns'] < 0):
                raise ValueError('Invalid choice record')
        return data

    def _load_choices(self):
        for name in ('choices.json', 'choices.json.bak'):
            path = self.base / name
            if not path.exists():
                continue
            try:
                data = self._read_choices(path)
                self._choices = data['choices']
                return
            except CacheFormatError:
                raise
            except (OSError, ValueError, TypeError):
                self._choices_primary_valid = False
        # Keep unreadable bytes as-is. A new choice will write a repaired file
        # without copying the damaged primary over a still valid backup.

    def _save_choices(self, *, explicit_key=None):
        if self.read_only:
            return
        # Global user/program stores may have several readers/writers, unlike
        # the single-writer gallery store. Merge timestamped choices under an
        # OS lock so independent windows cannot replace each other's records.
        from .cache_locations import DirectoryWriterLock
        lock = None
        deadline = time.monotonic() + 2
        while lock is None:
            try:
                lock = DirectoryWriterLock(self.base / '.choices.lock')
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError('手动选择记录正由另一个窗口保存，请稍后重试。')
                time.sleep(.02)
        try:
            primary = self.base / 'choices.json'
            previous = None
            explicit = dict(self._choices[explicit_key]) if explicit_key is not None else None
            if primary.exists():
                try:
                    previous = self._read_choices(primary)
                    self._choices_primary_valid = True
                    for key, value in previous['choices'].items():
                        current = self._choices.get(key)
                        if (isinstance(value, dict) and
                                (current is None or int(value.get('updated_ns', 0)) > int(current.get('updated_ns', 0)))):
                            self._choices[key] = value
                except (OSError, ValueError, TypeError) as error:
                    if isinstance(error, CacheFormatError):
                        raise
                    previous = None
                    self._choices_primary_valid = False
            if explicit is not None:
                latest = self._choices.get(explicit_key, {})
                explicit['updated_ns'] = max(time.time_ns(), int(explicit.get('updated_ns', 0)),
                                            int(latest.get('updated_ns', 0)) + 1)
                self._choices[explicit_key] = explicit
            if previous is not None and self._choices_primary_valid:
                _atomic_json(self.base / 'choices.json.bak', previous)
            _atomic_json(primary, {'schema': 1, 'choices': self._choices})
            self._choices_primary_valid = True
        finally:
            lock.close()

    def _override_record(self, item):
        key = self._item_key(item)
        record = self._choices.get(key)
        if isinstance(record, dict):
            try:
                path = None if record.get('path') is None else self._decode_path(record['path'])
                return path, int(record.get('updated_ns', 0))
            except (ValueError, TypeError):
                return None
        if self.library_root is not None:
            return None
        row = self._connection.execute('SELECT path FROM overrides WHERE item_id=?', (key,)).fetchone()
        return (Path(row[0]), 0) if row else None

    def get_override(self, item):
        with self._lock:
            self._check_open()
            key = self._item_key(item)
            self._remember_request('items', key,
                                   (self._item_version(item, ''), self._decision_revisions.get(key, 0)))
            record = self._override_record(item)
        for source in self._sources():
            try:
                with source._lock:
                    candidate = source._override_record(item)
                if candidate is not None and (record is None or candidate[1] > record[1]):
                    record = candidate
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                pass
        if record is None:
            return None
        selected, timestamp = record
        candidates = item.candidates or (item.image_path,)
        if selected is not None and (selected not in candidates or not selected.is_file()):
            return None
        if not self.read_only:
            with self._lock:
                existing = self._choices.get(key)
                if existing is None or int(existing.get('updated_ns', 0)) < timestamp:
                    self._choices[key] = {'path': None if selected is None else self._path(selected), 'updated_ns': timestamp}
                    self._save_choices()
        return selected

    def clear_thumbnails(self):
        if self.read_only:
            raise OSError('此缓存当前为只读。')
        reclaimed = 0
        with self._lock:
            self._check_open()
            self._safe_thumbnails()
            self._touches.clear()
            rows = self._connection.execute('SELECT key,filename FROM thumbnails').fetchall()
            for key, filename in rows:
                size = self._unlink_thumbnail(key, filename)
                if size is None:
                    continue
                reclaimed += size
                self._connection.execute('DELETE FROM thumbnails WHERE key=?', (key,))
            self._connection.commit()
        return reclaimed

    def set_override(self, item, path):
        if self.read_only:
            raise OSError('此缓存当前为只读。')
        if path is not None and Path(path) not in (item.candidates or (item.image_path,)):
            raise ValueError('The selected file must be an image candidate for this item')
        # An explicit new choice must supersede imported timestamps too, even
        # if another device's clock was ahead of this machine's current clock.
        timestamp = time.time_ns()
        for source in self._sources():
            try:
                with source._lock:
                    record = source._override_record(item)
                if record is not None:
                    timestamp = max(timestamp, record[1] + 1)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                pass
        with self._lock:
            self._check_open()
            key = self._item_key(item)
            self._decision_revisions[key] = self._decision_revisions.get(key, 0) + 1
            previous = self._choices.get(key, {})
            timestamp = max(timestamp, int(previous.get('updated_ns', 0)) + 1)
            self._choices[key] = {'path': None if path is None else self._path(path), 'updated_ns': timestamp}
            self._save_choices(explicit_key=key)
            if self.library_root is None and path is None:
                self._connection.execute('DELETE FROM overrides WHERE item_id=?', (key,))
            elif self.library_root is None:
                self._connection.execute('INSERT OR REPLACE INTO overrides VALUES (?,?)', (key, str(path)))
            self._connection.execute('DELETE FROM decisions WHERE item_id=?', (key,))
            self._connection.commit()

    def forget_decision(self, item):
        if self.read_only:
            return
        with self._lock:
            key = self._item_key(item)
            self._decision_revisions[key] = self._decision_revisions.get(key, 0) + 1
            self._connection.execute('DELETE FROM decisions WHERE item_id=?', (key,))
            self._connection.commit()

    def invalidate_item(self, item, previous_versions=()):
        if self.read_only:
            return
        with self._lock:
            self._check_open()
            self._safe_thumbnails()
            key = self._item_key(item)
            self._decision_revisions[key] = self._decision_revisions.get(key, 0) + 1
            self._connection.execute('DELETE FROM decisions WHERE item_id=?', (key,))
            for previous in previous_versions:
                version = previous
                if self.library_root is not None:
                    try:
                        version = (self._path(previous[0]), *previous[1:])
                    except ValueError:
                        continue
                # Also discard a revision-qualified entry from a prior deep
                # verification or an unchanged-size manual cover replacement.
                for row in self._connection.execute('SELECT key,filename FROM thumbnails').fetchall():
                    if row[0] == self._thumbnail_key(previous[0], 256, version):
                        if self._unlink_thumbnail(row[0], row[1]) is None:
                            continue
                        self._connection.execute('DELETE FROM thumbnails WHERE key=?', (row[0],))
            # Bump the content generation even when an editor preserves mtime.
            for path in item.candidates or (item.image_path,):
                self._bump_file(path)
            self._connection.commit()

    def _bump_file(self, path):
        name, size, modified = file_version(path)
        key = self._path(path)
        self._connection.execute('''INSERT INTO file_records(path,size,mtime_ns,digest,revision)
            VALUES (?,?,?,NULL,1) ON CONFLICT(path) DO UPDATE SET
            revision=revision+1,size=excluded.size,mtime_ns=excluded.mtime_ns,digest=NULL''',
            (key, size, modified))

    def get_decision(self, item, algorithm):
        with self._lock:
            self._check_open()
            key = self._item_key(item)
            version = self._item_version(item, algorithm)
            self._remember_request('versions', (key, algorithm),
                                   (version, self._decision_revisions.get(key, 0)))
            row = self._connection.execute('SELECT version,payload FROM decisions WHERE item_id=?', (key,)).fetchone()
        if row and row[0] == version:
            try:
                data = json.loads(row[1])
                selected = self._decode_path(data['image_path'])
                extras = tuple(self._decode_path(path) for path in data['extra_images'])
                candidates = item.candidates or (item.image_path,)
                if (selected not in candidates or not selected.is_file()
                        or any(path not in candidates or not path.is_file() for path in extras)):
                    return None
                return PairDecision(selected, extras, data['uncertain'], data['reason'], data.get('same_content', False))
            except (ValueError, KeyError, TypeError):
                return None
        with self._lock:
            paths = (*item.candidates, item.image_path, item.video_path)
            bumped = any(path is not None and len(self._version(path)) > 3 for path in paths)
            local_choice = key in self._choices
            superseded = bumped or local_choice or self._decision_revisions.get(key, 0) > 0
        for source in (() if superseded else self._sources()):
            try:
                result = source.get_decision(item, algorithm)
                if result is not None:
                    if not self.read_only:
                        self.put_decision(item, algorithm, result, _record_fingerprints=False)
                        for path in (*item.candidates, item.image_path, item.video_path):
                            if path is not None:
                                self._import_fingerprint(source, path)
                        with self._lock:
                            self._connection.commit()
                    return result
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                pass
        return None

    def put_decision(self, item, algorithm, decision, *, _record_fingerprints=True):
        if self.read_only:
            return
        key = self._item_key(item)
        payload = json.dumps({'image_path': self._path(decision.image_path),
                              'extra_images': [self._path(path) for path in decision.extra_images],
                              'uncertain': decision.uncertain, 'reason': decision.reason,
                              'same_content': decision.same_content}, ensure_ascii=False)
        with self._lock:
            self._check_open()
            version = self._item_version(item, algorithm)
            requested = getattr(self._requests, 'versions', {}).pop((key, algorithm), None)
            if requested is not None and requested != (version, self._decision_revisions.get(key, 0)):
                return
            started = getattr(self._requests, 'items', {}).pop(key, None)
            if started is not None and started != (self._item_version(item, ''), self._decision_revisions.get(key, 0)):
                return
            self._connection.execute('INSERT OR REPLACE INTO decisions VALUES (?,?,?)', (key, version, payload))
            self._connection.commit()
        if _record_fingerprints:
            for path in (*item.candidates, item.image_path, item.video_path):
                if path is not None:
                    self._record_file(path)
        with self._lock:
            self._connection.commit()

    def _remember_request(self, name, key, value):
        values = getattr(self._requests, name, None)
        if values is None:
            values = {}
            setattr(self._requests, name, values)
        values[key] = value
        while len(values) > 32:
            values.pop(next(iter(values)))

    def verify_files(self, paths, cancel=None, progress=None):
        """Read full file bytes only after explicit user-requested deep checking.

        A prior full fingerprint is recorded while cache results are generated
        for ordinary Live Photo sized files. If none exists, this establishes
        the baseline and conservatively invalidates any derived result once.
        """
        if self.read_only:
            raise OSError('此缓存当前为只读。')
        checked, changed = 0, []
        paths = tuple(dict.fromkeys(Path(path) for path in paths))
        for path in paths:
            if cancel and cancel():
                return {'checked': checked, 'changed': changed, 'cancelled': True}
            try:
                digest = _content_hash(path, cancel)
                version = file_version(path)
                with self._lock:
                    self._check_open()
                    key = self._path(path)
                    row = self._connection.execute('SELECT size,mtime_ns,digest FROM file_records WHERE path=?', (key,)).fetchone()
                    different = row is None or row != (version[1], version[2], digest)
                    if different:
                        self._bump_file(path)
                        # Pair decisions have hashes of every candidate/video;
                        # bumping this file automatically expires only groups
                        # which reference it. Preserve all user choices.
                        changed.append(path)
                    self._connection.execute('''INSERT INTO file_records(path,size,mtime_ns,digest,revision)
                        VALUES (?,?,?,?,0) ON CONFLICT(path) DO UPDATE SET
                        size=excluded.size,mtime_ns=excluded.mtime_ns,digest=excluded.digest''',
                        (key, version[1], version[2], digest))
                    self._connection.commit()
                checked += 1
                if progress:
                    progress(checked, len(paths))
            except InterruptedError:
                return {'checked': checked, 'changed': changed, 'cancelled': True}
        return {'checked': checked, 'changed': changed, 'cancelled': False}

    def close(self):
        callback = None
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._flush_touches()
                self._connection.close()
            self._closed = True
            callback, self._close_callback = self._close_callback, None
        for source in self._owned_readers:
            source.close()
        if callback is not None:
            callback()
