"""Independent full-resolution, exact-frame Save As transactions."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import uuid

from PIL import Image

from .cover_save import (CoverSaveError, FileFingerprint, SourceChangedError,
                         _cancelled, _encode_image, _publish_no_replace,
                         _stat_key, _work_root, _writable)
from .frame_reader import FrameReader


class FrameExportError(CoverSaveError):
    pass


class _CallbackCancellation:
    def __init__(self, callback):
        self.callback = callback

    def is_set(self):
        return bool(self.callback())


def _encode_export(image, target, cancel_event=None, heic_encoder=None):
    if target.suffix.casefold() != '.png':
        _encode_image(image, target, cancel_event, heic_encoder)
        return
    _cancelled(cancel_event)
    image.convert('RGB').save(target, format='PNG')
    with target.open('r+b') as stream:
        stream.flush()
        os.fsync(stream.fileno())
    _cancelled(cancel_event)


def _validate_export(image, target):
    suffix = target.suffix.casefold()
    with target.open('rb') as stream:
        header = stream.read(128)
    if suffix == '.png' and not header.startswith(b'\x89PNG\r\n\x1a\n'):
        raise FrameExportError('PNG 图片格式校验失败，未更改目标文件。')
    if suffix in ('.jpg', '.jpeg') and not header.startswith(b'\xff\xd8\xff'):
        raise FrameExportError('JPEG 图片格式校验失败，未更改目标文件。')
    if suffix == '.heic':
        brands = (b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'hevm', b'hevs')
        if header[4:8] != b'ftyp' or not any(brand in header[8:] for brand in brands):
            raise FrameExportError('导出结果不是有效 HEIC，未更改目标文件。')
    # Register the portable HEIC reader, then fully decode the staged image.
    from . import imaging  # noqa: F401
    with Image.open(target) as decoded:
        expected_format = 'PNG' if suffix == '.png' else 'JPEG' if suffix in ('.jpg', '.jpeg') else 'HEIF'
        if decoded.format != expected_format or decoded.mode not in ('RGB', 'RGBA'):
            raise FrameExportError('导出图片格式或颜色模式校验失败，未更改目标文件。')
        if decoded.size != image.size:
            raise FrameExportError('导出图片尺寸校验失败，未更改目标文件。')
        decoded.load()


def export_frame(video_path, index, destination, *, expected_video=None,
                 cancel_event=None, reader_factory=FrameReader, heic_encoder=None):
    """Export one displayed decoder index without altering its Live Photo pair.

    Call on an independent worker. Pass the editor-open FileFingerprint as
    ``expected_video`` to reject a video replaced since the displayed preview.
    Without it, changes are still checked throughout this export transaction.
    Cancellation may be a threading.Event or a no-argument callback. Once the
    atomic publication succeeds, the export is committed and returns success.
    """
    if type(index) is not int or index < 0:
        raise ValueError('请选择有效的视频帧。')
    video_path, destination = Path(video_path), Path(destination)
    if destination.suffix.casefold() not in ('.png', '.jpg', '.jpeg', '.heic'):
        raise FrameExportError('请选择 PNG、JPG、JPEG 或 HEIC 图片格式。')
    if callable(cancel_event) and not hasattr(cancel_event, 'is_set'):
        cancel_event = _CallbackCancellation(cancel_event)
    _cancelled(cancel_event)
    parent = destination.parent
    _writable(parent)
    if destination.is_symlink() or getattr(destination, 'is_junction', lambda: False)():
        raise FrameExportError('目标是链接，请选择实际图片文件。')
    if destination.exists() and os.path.samefile(destination, video_path):
        raise FrameExportError('不能使用导出图片覆盖源视频。')
    previous = FileFingerprint.capture(destination, cancel_event) if destination.exists() else None
    if previous is not None:
        _writable(destination)
    source = expected_video or FileFingerprint.capture(video_path, cancel_event)
    if source.path.resolve() != video_path.resolve():
        raise SourceChangedError('视频与当前预览来源不一致，请重新打开编辑器。')
    if expected_video is not None:
        source.validate(cancel_event)
    reader = reader_factory(video_path, cancel_event=cancel_event)
    try:
        image = reader.export(index)
        image.load()
    finally:
        reader.close()
    _cancelled(cancel_event)
    if image.width < 1 or image.height < 1:
        raise FrameExportError('当前视频帧为空，无法导出。')
    root = _work_root(parent)
    transaction = root / ('export-' + uuid.uuid4().hex)
    transaction.mkdir()
    try:
        staged = transaction / ('frame' + destination.suffix)
        _encode_export(image, staged, cancel_event, heic_encoder)
        _validate_export(image, staged)
        source.validate(cancel_event)
        if previous is not None:
            previous.validate(cancel_event)
        _cancelled(cancel_event)
        if _stat_key(source.path.stat()) != source.stat_key:
            raise SourceChangedError('提交前视频发生变化，请重新打开编辑器。')
        if previous is None:
            _publish_no_replace(staged, destination)
        else:
            if _stat_key(destination.stat()) != previous.stat_key:
                raise FrameExportError('目标文件已被其他程序更改，请重新选择保存位置。')
            os.replace(staged, destination)
        return destination
    finally:
        shutil.rmtree(transaction, ignore_errors=True)
