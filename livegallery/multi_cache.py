"""Root-aware cache ownership for a merged collection of photo folders."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading

from .cache import CacheStore
from .cache_locations import (CACHE_DIRECTORY, MANIFEST_NAME, SCHEMA_VERSION,
                              open_cache, read_manifest)


def _path_key(path):
    value = os.path.normpath(os.path.abspath(os.fspath(path)))
    return value.casefold() if os.name == 'nt' else value


def _normalise(paths, canonical):
    if isinstance(paths, (str, os.PathLike)):
        paths = (paths,)
    output, seen = [], set()
    for value in paths:
        original = Path(os.path.abspath(Path(value).expanduser()))
        try:
            resolved = original.resolve(strict=False)
        except OSError:
            resolved = original
        key = _path_key(resolved)
        if key not in seen:
            output.append(resolved if canonical else original)
            seen.add(key)
    return tuple(output)


def normalise_roots(paths, recursive=True):
    """Keep selected nested roots, dropping only canonical path duplicates."""
    return _normalise(paths, canonical=True)


def scan_roots(paths, recursive=True):
    """Avoid scanning a selected descendant twice when recursion is enabled."""
    roots = normalise_roots(paths, recursive)
    if not recursive:
        return roots
    return tuple(root for root in roots if not any(
        root != other and root.is_relative_to(other) for other in roots))


def _source_paths(sources):
    output, seen = [], set()
    for source in sources:
        if hasattr(source, 'actual_paths'):
            values = source.actual_paths
        elif isinstance(source, CacheStore):
            values = (source.base,)
        else:
            values = (Path(source),)
        for path in values:
            path = Path(path)
            key = _path_key(path)
            if key not in seen:
                output.append(path)
                seen.add(key)
    return tuple(output)


def _sources_for(root, sources):
    """Never reinterpret another selected root's portable relative records."""
    selected_id = None
    try:
        selected_id = read_manifest(root / CACHE_DIRECTORY).get('library_id')
    except OSError:
        pass
    identity = str(root.absolute())
    if os.name == 'nt':
        identity = identity.casefold()
    scope = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    output = []
    for path in sources:
        if not (path / MANIFEST_NAME).exists():
            output.append(path)  # The original global caches use absolute keys.
            continue
        try:
            marker = read_manifest(path)
        except OSError:
            continue
        if marker.get('schema') != SCHEMA_VERSION or marker.get('thumbnail_format', 3) != 3:
            continue
        if marker.get('storage') == 'fallback':
            if marker.get('root_scope') != scope:
                continue
            if selected_id is not None and marker.get('origin_library_id') != selected_id:
                continue
        elif path.name.casefold() != CACHE_DIRECTORY.casefold() or _path_key(path.parent) != _path_key(root):
            continue
        output.append(path)
    return tuple(output)


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


class MultiRootCache:
    """Dispatch each job to its deepest selected root, owning all bindings.

    Jobs capture this facade as their owner. close() rejects new calls and
    waits for active calls before closing stores; callers should retire it on
    a background reaper after cancelling/waiting queued jobs, as for CacheStore.
    """

    library_root = None

    def __init__(self, roots, bindings, max_bytes):
        self.roots = self.library_roots = tuple(roots)
        self._bindings = tuple(bindings)
        self._by_root = dict(zip(self.roots, self._bindings))
        self._routing = sorted(self.roots, key=lambda root: len(root.parts), reverse=True)
        self.base = self._bindings[0].actual_path
        self.actual_paths = tuple(dict.fromkeys(binding.actual_path for binding in self._bindings))
        self.recovery_warning = '\n'.join(f'{root}: {binding.cache.recovery_warning}'
                                          for root, binding in self._by_root.items()
                                          if binding.cache.recovery_warning)
        self._condition = threading.Condition(threading.RLock())
        self._active = 0
        self._closing = False
        self._closed = False
        self._max_bytes = max(0, int(max_bytes))
        self._apply_budgets()

    @property
    def writer_roots(self):
        if self._closed:
            return ()
        return tuple(root for root, binding in self._by_root.items()
                     if binding.location == 'library' and not binding.fallback)

    @property
    def fallback_roots(self):
        return tuple(root for root, binding in self._by_root.items() if binding.fallback)

    @property
    def has_fallbacks(self):
        return bool(self.fallback_roots)

    @property
    def max_bytes(self):
        return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value):
        with self._condition:
            self._max_bytes = max(0, int(value))
            # A settings change can happen while an old owner is being retired.
            if not self._closing and not self._closed:
                self._apply_budgets()

    def _apply_budgets(self):
        # Several root-aware global handles share one DB/thumbnail directory.
        # Count that directory once, while independent gallery caches divide B.
        groups = {}
        for binding in self._bindings:
            groups.setdefault(_path_key(binding.actual_path), []).append(binding.cache)
        quotient, remainder = divmod(self._max_bytes, len(groups))
        for number, stores in enumerate(groups.values()):
            budget = quotient + (1 if number < remainder else 0)
            for store in stores:
                store.max_bytes = budget

    @contextmanager
    def _lease(self):
        with self._condition:
            if self._closing or self._closed:
                raise RuntimeError('缓存已关闭。')
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def root_for(self, path_or_item):
        source = getattr(path_or_item, 'video_path', None) or getattr(path_or_item, 'image_path', path_or_item)
        path = Path(os.path.abspath(source))
        for root in self._routing:
            if path.is_relative_to(root):
                return root
        raise ValueError('文件不属于当前选择的照片文件夹。')

    def _store_for(self, path_or_item):
        return self._by_root[self.root_for(path_or_item)].cache

    def base_for(self, path_or_item):
        return self._store_for(path_or_item).base

    def get_thumbnail(self, path, edge=256):
        with self._lease():
            return self._store_for(path).get_thumbnail(path, edge)

    def get_override(self, item):
        with self._lease():
            return self._store_for(item).get_override(item)

    def set_override(self, item, path):
        with self._lease():
            return self._store_for(item).set_override(item, path)

    def get_decision(self, item, algorithm):
        with self._lease():
            return self._store_for(item).get_decision(item, algorithm)

    def put_decision(self, item, algorithm, decision, *, _record_fingerprints=True):
        with self._lease():
            return self._store_for(item).put_decision(
                item, algorithm, decision, _record_fingerprints=_record_fingerprints)

    def forget_decision(self, item):
        with self._lease():
            return self._store_for(item).forget_decision(item)

    def invalidate_item(self, item, previous_versions=()):
        with self._lease():
            return self._store_for(item).invalidate_item(item, previous_versions)

    def clear_thumbnails(self):
        with self._lease():
            reclaimed, seen = 0, set()
            for binding in self._bindings:
                key = _path_key(binding.actual_path)
                if key not in seen:
                    reclaimed += binding.cache.clear_thumbnails()
                    seen.add(key)
            return reclaimed

    def verify_files(self, paths, cancel=None, progress=None):
        with self._lease():
            groups, seen = {}, set()
            for value in paths:
                path = Path(value)
                key = _path_key(path)
                if key not in seen:
                    groups.setdefault(self.root_for(path), []).append(path)
                    seen.add(key)
            total, checked, changed = len(seen), 0, []
            for root, values in groups.items():
                if _cancelled(cancel):
                    return {'checked': checked, 'changed': changed, 'cancelled': True}
                report = self._by_root[root].cache.verify_files(
                    values, cancel=(lambda: _cancelled(cancel)),
                    progress=(lambda done, _count, offset=checked: progress(offset + done, total))
                    if progress is not None else None)
                checked += report['checked']
                changed.extend(report['changed'])
                if report['cancelled']:
                    return {'checked': checked, 'changed': changed, 'cancelled': True}
            return {'checked': checked, 'changed': changed, 'cancelled': False}

    def close(self):
        with self._condition:
            if self._closed:
                return
            if self._closing:
                self._condition.wait_for(lambda: self._closed)
                return
            self._closing = True
            self._condition.wait_for(lambda: self._active == 0)
        failure = None
        try:
            for binding in self._bindings:
                try:
                    binding.close()
                except Exception as error:
                    failure = failure or error
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()
        if failure is not None:
            raise failure


@dataclass
class MultiCacheBinding:
    cache: MultiRootCache
    warning: str
    location: str
    requested_location: str

    @property
    def actual_path(self):
        return self.cache.base

    @property
    def actual_paths(self):
        return self.cache.actual_paths

    @property
    def fallback(self):
        return self.cache.has_fallbacks

    def close(self):
        self.cache.close()


def open_multi_cache(location, roots, *, user_base, program_base,
                     max_bytes=2 * 1024 ** 3, legacy_sources=(), cancel=None):
    """Open on a worker; a partial open or cancellation releases owned locks."""
    # A scanner already using a lexical alias (e.g. macOS /var vs /private/var)
    # must keep the same namespace as its MediaItem paths. The application
    # canonicalizes selections with normalise_roots before scanning; direct
    # callers retain their first supplied spelling while aliases deduplicate.
    roots = _normalise(roots, canonical=False)
    if _cancelled(cancel):
        raise InterruptedError('已取消缓存准备。')
    if len(roots) <= 1:
        binding = open_cache(location, roots[0] if roots else None, user_base=user_base,
                             program_base=program_base, max_bytes=max_bytes, legacy_sources=legacy_sources)
        if _cancelled(cancel):
            binding.close()
            raise InterruptedError('已取消缓存准备。')
        return binding
    sources = _source_paths(legacy_sources)
    bindings = []
    quotient, remainder = divmod(max(0, int(max_bytes)), len(roots))
    try:
        for number, root in enumerate(roots):
            if _cancelled(cancel):
                raise InterruptedError('已取消缓存准备。')
            budget = quotient + (1 if number < remainder else 0) if location == 'library' else max_bytes
            bindings.append(open_cache(location, root, user_base=user_base, program_base=program_base,
                                       max_bytes=budget, legacy_sources=_sources_for(root, sources)))
        if _cancelled(cancel):
            raise InterruptedError('已取消缓存准备。')
        cache = MultiRootCache(roots, bindings, max_bytes)
    except BaseException:
        for binding in bindings:
            try:
                binding.close()
            except Exception:
                pass
        raise
    actual = {binding.location for binding in bindings}
    resolved = next(iter(actual)) if len(actual) == 1 else location
    warnings = '\n'.join(f'{root}: {binding.warning}' for root, binding in zip(roots, bindings) if binding.warning)
    return MultiCacheBinding(cache, warnings, resolved, location)


open_caches = open_multi_cache
