"""Fast, metadata-only recursive indexing. Expensive comparisons run separately."""
import hashlib
import os
import re
from pathlib import Path
from typing import Callable

from .models import MediaItem

LIVE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".heic"})
IMAGE_EXTENSIONS = LIVE_EXTENSIONS | frozenset({".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"})


def natural_key(value: str | Path) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", str(value)))


def preferred_image(paths: list[Path] | tuple[Path, ...]) -> Path:
    return min(paths, key=lambda p: (p.suffix.casefold() != ".heic", natural_key(p.name), p.name))


def _identifier(kind: str, path: Path) -> str:
    return hashlib.sha256((kind + "\0" + str(path.absolute())).encode("utf-8")).hexdigest()[:24]


def scan_folder(root: Path, cancel: Callable[[], bool] | None = None,
                progress: Callable[[int], None] | None = None, *,
                recursive: bool = True) -> list[MediaItem]:
    root = Path(root).absolute()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    items: list[MediaItem] = []
    count = 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        if cancel and cancel():
            break
        parent = Path(directory)
        directories[:] = (sorted((name for name in directories
                                  if name.casefold() != ".liveviewer-work"
                                  and not (parent / name).is_symlink()), key=natural_key)
                          if recursive else [])
        images: dict[str, list[Path]] = {}
        videos: dict[str, list[Path]] = {}
        ordinary: list[Path] = []
        for filename in sorted(filenames, key=natural_key):
            if cancel and cancel():
                break
            path = parent / filename
            suffix = path.suffix.casefold()
            stem = path.stem.casefold()
            if suffix in LIVE_EXTENSIONS:
                images.setdefault(stem, []).append(path)
                count += 1
            elif suffix in IMAGE_EXTENSIONS:
                ordinary.append(path)
                count += 1
            elif suffix == ".mov":
                videos.setdefault(stem, []).append(path)
        for stem, candidates in images.items():
            matching = videos.get(stem)
            if matching:
                selected = preferred_image(candidates)
                items.append(MediaItem(
                    id=_identifier("live", parent / stem), image_path=selected,
                    video_path=matching[0], candidates=tuple(candidates),
                    pending=len(candidates) > 1,
                    note="存在多个同名 MOV，使用文件名排序后的第一个" if len(matching) > 1 else ""))
            else:
                ordinary.extend(candidates)
        items.extend(MediaItem(_identifier("image", path), path) for path in ordinary)
        if progress:
            progress(count)
    items.sort(key=lambda item: natural_key(item.image_path.relative_to(root)))
    return items
