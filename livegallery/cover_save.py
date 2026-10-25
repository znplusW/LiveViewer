"""Transactional cover-image saves; originals stay intact until commit.

Stages share the source volume, under a directory excluded by the library
scanner. Overwrite deliberately retains no original-image backup. New copies
are published without replacing any existing file.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import MediaItem

WORK_DIRECTORY = ".LiveViewer-work"


class CoverSaveError(RuntimeError):
    pass


class SourceChangedError(CoverSaveError):
    pass


def _cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("已取消保存，原文件未更改。")


def _stat_key(value) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@dataclass(frozen=True)
class FileFingerprint:
    path: Path
    stat_key: tuple[int, int, int, int, int]
    sha256: str

    @classmethod
    def capture(cls, path: Path, cancel_event=None) -> "FileFingerprint":
        path = Path(path)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or path.is_symlink():
            raise CoverSaveError("仅支持编辑普通本地文件，请选择实际图片和视频文件。")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                _cancelled(cancel_event)
                digest.update(chunk)
        _cancelled(cancel_event)
        after = path.stat()
        if _stat_key(before) != _stat_key(after):
            raise SourceChangedError("文件在读取期间被其他程序修改，请关闭编辑器后重新打开。")
        return cls(path, _stat_key(after), digest.hexdigest())

    def validate(self, cancel_event=None) -> None:
        try:
            if _stat_key(self.path.stat()) != self.stat_key:
                raise SourceChangedError("图片或视频已被其他程序修改，请重新打开编辑器。")
            current = FileFingerprint.capture(self.path, cancel_event)
        except InterruptedError:
            raise
        except OSError as error:
            raise SourceChangedError("源文件已移动或无法访问，请检查硬盘连接并重新打开编辑器。") from error
        if current != self:
            raise SourceChangedError("图片或视频内容已发生变化，请重新打开编辑器。")


@dataclass(frozen=True)
class CoverSource:
    image: FileFingerprint
    video: FileFingerprint

    @classmethod
    def capture(cls, item: MediaItem, cancel_event=None) -> "CoverSource":
        if item.video_path is None:
            raise CoverSaveError("此项目没有对应视频，无法更改封面图。")
        image, video = Path(item.image_path), Path(item.video_path)
        if image.suffix.casefold() not in (".jpg", ".jpeg", ".heic"):
            raise CoverSaveError("目前仅支持 JPG、JPEG 和 HEIC 封面图。")
        if image.parent.resolve() != video.parent.resolve():
            raise CoverSaveError("图片和视频必须位于同一文件夹。")
        if image.stem.casefold() != video.stem.casefold():
            raise CoverSaveError("图片和视频文件名必须相同，才能保持 Live Photo 配对。")
        return cls(FileFingerprint.capture(image, cancel_event),
                   FileFingerprint.capture(video, cancel_event))

    def validate(self, cancel_event=None) -> None:
        self.video.validate(cancel_event)
        self.image.validate(cancel_event)

    def validate_identity(self) -> None:
        # A video hash may take longer on a mechanical drive. Recheck both
        # identities immediately before publication, after all lengthy work.
        for fingerprint in (self.image, self.video):
            if _stat_key(fingerprint.path.stat()) != fingerprint.stat_key:
                raise SourceChangedError("提交前检测到源文件变化，已取消保存，请重新打开编辑器。")


@dataclass(frozen=True)
class SaveResult:
    image_path: Path
    video_path: Path
    mode: str
    backup_path: Path | None
    source_image_path: Path
    source_video_path: Path

    @property
    def created_copy(self) -> bool:
        return self.mode == "copy"


def _writable(path: Path) -> None:
    status = path.stat()
    # Windows directory READONLY is also used for folder customization. It
    # is not an ACL write denial; creating the private stage and publishing
    # the result provide the actual permission checks. Keep the guard for
    # Windows files and POSIX directories/files where these bits apply.
    if os.name == 'nt' and stat.S_ISDIR(status.st_mode):
        return
    if not status.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise CoverSaveError("目标文件或文件夹为只读，未更改任何原文件。")


def _work_root(parent: Path) -> Path:
    root = parent / WORK_DIRECTORY
    if root.is_symlink():
        raise CoverSaveError("编辑临时目录是符号链接，请先更换或移除此目录。")
    root.mkdir(exist_ok=True)
    _writable(root)
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        api.GetFileAttributesW.restype = ctypes.c_uint32
        api.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        api.SetFileAttributesW.restype = ctypes.c_int
        attributes = api.GetFileAttributesW(str(root))
        if attributes != 0xFFFFFFFF:
            api.SetFileAttributesW(str(root), attributes | 0x2)  # HIDDEN
    return root


def _copy_checked(source: FileFingerprint, target: Path, cancel_event=None) -> None:
    digest = hashlib.sha256()
    with source.path.open("rb") as reader, target.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            _cancelled(cancel_event)
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if digest.hexdigest() != source.sha256 or _stat_key(source.path.stat()) != source.stat_key:
        raise SourceChangedError("复制期间源文件发生变化，已取消保存。")
    shutil.copystat(source.path, target)


def _encode_image(image: Image.Image, target: Path, cancel_event=None, heic_encoder=None) -> None:
    _cancelled(cancel_event)
    if target.suffix.casefold() == ".heic":
        if heic_encoder is None:
            try:
                from .heic_encoder import encode_heic
            except ImportError as error:
                raise CoverSaveError("HEIC 编码组件不可用，未保存或转换任何文件。") from error
            heic_encoder = encode_heic
        heic_encoder(image, target, cancel_event=cancel_event)
    else:
        image.convert("RGB").save(target, format="JPEG", quality=95, subsampling=0)
    _cancelled(cancel_event)
    # Reject mislabeled JPEG/AVIF payloads even if an image decoder can read them.
    with target.open("r+b") as encoded:
        header = encoded.read(128)
        os.fsync(encoded.fileno())
    if target.suffix.casefold() == ".heic":
        brands = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs")
        if header[4:8] != b"ftyp" or not any(brand in header[8:] for brand in brands):
            raise CoverSaveError("编码结果不是有效 HEIC 文件，原文件未更改。")
    elif not header.startswith(b"\xff\xd8\xff"):
        raise CoverSaveError("JPEG 编码校验失败，原文件未更改。")
    from .imaging import load_image
    decoded = load_image(target, None)
    if decoded.size != image.size:
        raise CoverSaveError("保存后的图片尺寸校验失败，原文件未更改。")


def _publish_no_replace(staged: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows rename is atomic and raises FileExistsError on an existing
        # target; unlike os.replace, it cannot overwrite a concurrent creator.
        os.rename(staged, target)
    else:
        # POSIX rename may replace an existing target. Hard-link publication
        # provides the required exclusive creation on the same source volume.
        os.link(staged, target)
        staged.unlink()


def _same_stem_files(parent: Path, stem: str) -> list[Path]:
    folded = stem.casefold()
    return [path for path in parent.iterdir() if path.stem.casefold() == folded]


def _reserve_copy(source: CoverSource, root: Path) -> tuple[Path, Path, Path]:
    parent, base = source.image.path.parent, source.image.path.stem
    occupied = {path.stem.casefold() for path in parent.iterdir()}
    for number in range(1, 100_001):
        stem = f"{base} v{number}"
        if stem.casefold() in occupied:
            continue
        token = hashlib.sha256(stem.casefold().encode("utf-8")).hexdigest()
        reservation = root / f".reserve-{token}"
        try:
            descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        try:
            if _same_stem_files(parent, stem):
                reservation.unlink()
                continue
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
        return (parent / (stem + source.image.path.suffix),
                parent / (f"{source.video.path.stem} v{number}" + source.video.path.suffix), reservation)
    raise CoverSaveError("副本版本号过多，无法分配新的文件名。")


def _rollback_published(published: list[FileFingerprint]) -> None:
    changed = []
    for output in reversed(published):
        path = output.path
        try:
            # Link/rename can update ctime, so compare the stable inode plus
            # content hash. Never remove an output another program replaced or
            # edited while this transaction was being rolled back.
            current = FileFingerprint.capture(path)
            if current.stat_key[:2] != output.stat_key[:2] or current.sha256 != output.sha256:
                changed.append(path.name)
                continue
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, CoverSaveError):
            changed.append(path.name)
    if changed:
        raise CoverSaveError("保存未完成，部分输出被其他程序占用或修改，已保留：" + "、".join(changed))


def save_cover(source: CoverSource, image: Image.Image, mode: str = "copy", *,
               cancel_event: threading.Event | None = None,
               heic_encoder: Callable | None = None) -> SaveResult:
    """Commit a selected, already oriented full-resolution video frame.

    Cancellation before commit leaves originals unchanged. Once the atomic
    overwrite (or both exclusive copy publications) succeeds, the result is
    committed and reported as success rather than misreporting a late cancel.
    """
    if mode not in ("copy", "overwrite"):
        raise ValueError("Unknown cover save mode")
    if image.width < 1 or image.height < 1:
        raise CoverSaveError("所选视频帧为空，无法保存。")
    source.validate(cancel_event)
    parent = source.image.path.parent
    _writable(parent)
    if mode == "overwrite":
        _writable(source.image.path)
    root = _work_root(parent)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    transaction = root / identifier
    transaction.mkdir()
    reservation = None
    published: list[FileFingerprint] = []
    try:
        staged_image = transaction / ("cover" + source.image.path.suffix)
        _encode_image(image, staged_image, cancel_event, heic_encoder)
        if mode == "overwrite":
            source.validate(cancel_event)
            _cancelled(cancel_event)
            source.validate_identity()
            os.replace(staged_image, source.image.path)
            return SaveResult(source.image.path, source.video.path, mode, None,
                              source.image.path, source.video.path)
        target_image, target_video, reservation = _reserve_copy(source, root)
        staged_video = transaction / ("video" + source.video.path.suffix)
        _copy_checked(source.video, staged_video, cancel_event)
        source.validate(cancel_event)
        _cancelled(cancel_event)
        source.validate_identity()
        if _same_stem_files(parent, target_image.stem):
            raise FileExistsError("副本文件名已被其他程序使用，请重新保存。")
        # Prepare ownership records before publication; no fallible filesystem
        # read may sit between a successful publication and rollback tracking.
        image_identity = FileFingerprint.capture(staged_image, cancel_event)
        video_identity = FileFingerprint(target_video, _stat_key(staged_video.stat()), source.video.sha256)
        _publish_no_replace(staged_image, target_image)
        published.append(FileFingerprint(target_image, image_identity.stat_key, image_identity.sha256))
        _cancelled(cancel_event)
        # Also catch a creator using a different extension for the same pair.
        conflicts = [path for path in _same_stem_files(parent, target_image.stem)
                     if path != target_image]
        if conflicts:
            raise FileExistsError("副本文件名已被其他程序使用，请重新保存。")
        _publish_no_replace(staged_video, target_video)
        published.append(video_identity)
        return SaveResult(target_image, target_video, mode, None,
                          source.image.path, source.video.path)
    except Exception:
        _rollback_published(published)
        raise
    finally:
        if reservation is not None:
            try:
                reservation.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(transaction, ignore_errors=True)
