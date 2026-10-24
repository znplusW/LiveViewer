"""Thread-safe persistent thumbnail, pairing, and manual-choice cache."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .imaging import thumbnail_bytes
from .models import MediaItem, PairDecision


def file_version(path: Path) -> tuple:
    path = Path(path)
    try:
        stat = path.stat()
        return (str(path.absolute()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (str(path.absolute()), None, None)


def item_version(item: MediaItem, algorithm: str) -> str:
    values = [file_version(p) for p in (item.candidates or (item.image_path,))]
    if item.video_path:
        values.append(file_version(item.video_path))
    return hashlib.sha256(json.dumps([algorithm, values], ensure_ascii=False).encode()).hexdigest()


class CacheStore:
    def __init__(self, base: Path, max_bytes: int = 2 * 1024 ** 3):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.thumbnail_dir = self.base / "thumbnails"
        self.thumbnail_dir.mkdir(exist_ok=True)
        self.max_bytes = max(0, int(max_bytes))
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(self.base / "library.sqlite3", timeout=20, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=20000")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS thumbnails (
                key TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL, used REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                item_id TEXT PRIMARY KEY, version TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS overrides (
                item_id TEXT PRIMARY KEY, path TEXT NOT NULL
            );
        """)
        self._evict(protected="")
        self._connection.commit()

    def get_thumbnail(self, path: Path, edge: int = 256) -> Path:
        key = hashlib.sha256(json.dumps(["thumbnail-v2", edge, file_version(path)]).encode()).hexdigest()
        target = self.thumbnail_dir / (key + ".jpg")
        with self._lock:
            if target.is_file():
                self._connection.execute("UPDATE thumbnails SET used=? WHERE key=?", (time.time(), key))
                self._connection.commit()
                return target
        content = thumbnail_bytes(path, edge)
        if len(content) > self.max_bytes:
            raise ValueError("The thumbnail exceeds the configured cache size limit")
        # Readers never see a partially written JPEG, even with concurrent requests.
        descriptor, temporary_name = tempfile.mkstemp(prefix="thumb-", suffix=".tmp", dir=self.thumbnail_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            with self._lock:
                # A competing request may already have produced this same key.
                # Reusing it avoids replacing a JPEG a Windows reader has open.
                if not target.is_file():
                    temporary.replace(target)
                self._connection.execute(
                    "INSERT OR REPLACE INTO thumbnails VALUES (?,?,?,?)", (key, target.name, len(content), time.time()))
                self._evict(protected=key)
                self._connection.commit()
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _evict(self, protected: str) -> None:
        total = self._connection.execute("SELECT COALESCE(SUM(size),0) FROM thumbnails").fetchone()[0]
        if total <= self.max_bytes:
            return
        rows = self._connection.execute(
            "SELECT key,filename,size FROM thumbnails WHERE key<>? ORDER BY used", (protected,)).fetchall()
        for key, filename, size in rows:
            try:
                (self.thumbnail_dir / filename).unlink(missing_ok=True)
            except OSError:
                continue
            self._connection.execute("DELETE FROM thumbnails WHERE key=?", (key,))
            total -= size
            if total <= self.max_bytes:
                break

    def get_override(self, item: MediaItem) -> Path | None:
        with self._lock:
            row = self._connection.execute("SELECT path FROM overrides WHERE item_id=?", (item.id,)).fetchone()
        if row:
            selected = Path(row[0])
            candidates = item.candidates or (item.image_path,)
            if selected in candidates and selected.is_file():
                return selected
        return None

    def clear_thumbnails(self) -> int:
        """Remove thumbnail files only; preserve analysis and manual choices.

        Returns reclaimed bytes. Files held open by another Windows process are
        retained and remain tracked for a later retry. Active image requests may
        naturally repopulate the cache after the clear operation.
        """
        reclaimed = 0
        with self._lock:
            for path in self.thumbnail_dir.glob("*.jpg"):
                try:
                    size = path.stat().st_size
                    path.unlink()
                    reclaimed += size
                except OSError:
                    continue
                self._connection.execute("DELETE FROM thumbnails WHERE filename=?", (path.name,))
            # Also discard records for files already removed outside the app.
            rows = self._connection.execute("SELECT key,filename FROM thumbnails").fetchall()
            for key, filename in rows:
                if not (self.thumbnail_dir / filename).exists():
                    self._connection.execute("DELETE FROM thumbnails WHERE key=?", (key,))
            self._connection.commit()
        return reclaimed

    def set_override(self, item: MediaItem, path: Path | None) -> None:
        if path is not None and Path(path) not in (item.candidates or (item.image_path,)):
            raise ValueError("The selected file must be an image candidate for this item")
        with self._lock:
            if path is None:
                self._connection.execute("DELETE FROM overrides WHERE item_id=?", (item.id,))
            else:
                self._connection.execute("INSERT OR REPLACE INTO overrides VALUES (?,?)", (item.id, str(path)))
            self._connection.execute("DELETE FROM decisions WHERE item_id=?", (item.id,))
            self._connection.commit()

    def forget_decision(self, item: MediaItem) -> None:
        """Invalidate automatic analysis while retaining the user's explicit choice."""
        with self._lock:
            self._connection.execute("DELETE FROM decisions WHERE item_id=?", (item.id,))
            self._connection.commit()

    def get_decision(self, item: MediaItem, algorithm: str) -> PairDecision | None:
        version = item_version(item, algorithm)
        with self._lock:
            row = self._connection.execute(
                "SELECT version,payload FROM decisions WHERE item_id=?", (item.id,)).fetchone()
        if not row or row[0] != version:
            return None
        try:
            data = json.loads(row[1])
            return PairDecision(Path(data["image_path"]), tuple(Path(p) for p in data["extra_images"]),
                                data["uncertain"], data["reason"], data.get("same_content", False))
        except (ValueError, KeyError, TypeError):
            return None

    def put_decision(self, item: MediaItem, algorithm: str, decision: PairDecision) -> None:
        payload = json.dumps({"image_path": str(decision.image_path),
                              "extra_images": [str(p) for p in decision.extra_images],
                              "uncertain": decision.uncertain, "reason": decision.reason,
                              "same_content": decision.same_content}, ensure_ascii=False)
        with self._lock:
            self._connection.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?)",
                                     (item.id, item_version(item, algorithm), payload))
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
