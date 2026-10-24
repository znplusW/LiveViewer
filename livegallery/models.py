"""Immutable values shared by indexing, media loading, and the interface."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaItem:
    id: str
    image_path: Path
    video_path: Path | None = None
    candidates: tuple[Path, ...] = ()
    pending: bool = False
    uncertain: bool = False
    note: str = ""

    @property
    def is_live(self) -> bool:
        return self.video_path is not None


@dataclass(frozen=True)
class PairDecision:
    image_path: Path
    extra_images: tuple[Path, ...]
    uncertain: bool
    reason: str
    same_content: bool = False
