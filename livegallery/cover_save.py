"""Transactional cover-image saves; originals stay intact until commit.

Stages share the source volume, under a directory excluded by the library
scanner. Both files are staged and checked before publication. A flushed transaction
journal and temporary originals allow rollback/recovery; successful saves
remove the transaction, including its temporary originals.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable

from PIL import Image

from .models import MediaItem

WORK_DIRECTORY = ".LiveViewer-work"


class CoverSaveError(RuntimeError):
    pass


class SourceChangedError(CoverSaveError):
    pass


def _is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def _safe_filename(name) -> bool:
    return (isinstance(name, str) and bool(name) and name not in ('.', '..')
            and not name.endswith((' ', '.')) and not PureWindowsPath(name).is_reserved()
            and not any(ord(character) < 32 or character in '<>:"/\\|?*' for character in name))


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
        if not stat.S_ISREG(before.st_mode) or _is_reparse(path):
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
        recover_cover_transactions(image.parent, cancel_event=cancel_event)
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
    asset_identifier: str | None = None
    photo_time: Fraction | None = None

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
    if _is_reparse(root):
        raise CoverSaveError("编辑临时目录是符号链接，请先更换或移除此目录。")
    root.mkdir(exist_ok=True)
    if _is_reparse(root):
        raise CoverSaveError("编辑临时目录是链接，请更换后重试。")
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


def _encode_image(image: Image.Image, target: Path, cancel_event=None, heic_encoder=None, *, exif=None) -> None:
    _cancelled(cancel_event)
    if target.suffix.casefold() == ".heic":
        if heic_encoder is None:
            try:
                from .heic_encoder import encode_heic
            except ImportError as error:
                raise CoverSaveError("HEIC 编码组件不可用，未保存或转换任何文件。") from error
            heic_encoder = encode_heic
        options = {'cancel_event': cancel_event}
        if exif is not None:
            options['exif'] = exif
        heic_encoder(image, target, **options)
    else:
        options = {} if exif is None else {"exif": exif}
        image.convert("RGB").save(target, format="JPEG", quality=95, subsampling=0, **options)
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


def _reserve_copy(source: CoverSource, root: Path) -> tuple[Path, Path, None]:
    # Caller holds the per-folder OS mutex. Exclusive publication still rejects
    # non-cooperating creators; no orphan reservation file can survive a crash.
    parent, base = source.image.path.parent, source.image.path.stem
    occupied = {path.stem.casefold() for path in parent.iterdir()}
    for number in range(1, 100_001):
        stem = f"{base} v{number}"
        if stem.casefold() not in occupied:
            return (parent / (stem + '.HEIC'),
                    parent / (f"{source.video.path.stem} v{number}" + source.video.path.suffix), None)
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


@contextmanager
def _parent_transaction_lock(root: Path, cancel_event=None):
    """One writer/recovery owner per folder; OS releases ownership on crash."""
    lock_path = root / ".commit.lock"
    deadline = time.monotonic() + 30
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        api.CreateMutexW.restype = ctypes.c_void_p
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.WaitForSingleObject.restype = ctypes.c_uint32
        api.ReleaseMutex.argtypes = [ctypes.c_void_p]
        api.ReleaseMutex.restype = ctypes.c_int
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
        # An OS mutex has no file target that could be replaced by a symlink.
        token = hashlib.sha256(str(root.resolve()).casefold().encode('utf-8')).hexdigest()
        handle = api.CreateMutexW(None, False, "Local\\LiveViewer-CoverSave-" + token)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            while True:
                _cancelled(cancel_event)
                result = api.WaitForSingleObject(handle, 25)
                if result in (0, 0x80):  # acquired, or previous process died
                    acquired = True
                    break
                if result != 0x102 or time.monotonic() >= deadline:
                    raise CoverSaveError("另一个保存或恢复操作正在占用此文件夹，请稍后重试。")
            yield
        finally:
            if acquired:
                api.ReleaseMutex(handle)
            api.CloseHandle(handle)
    else:
        import fcntl
        while True:
            _cancelled(cancel_event)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stream = os.fdopen(descriptor, "a+b")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.fstat(stream.fileno()).st_ino != lock_path.stat().st_ino:
                    stream.close()
                    continue
                break
            except BlockingIOError:
                stream.close()
                if time.monotonic() >= deadline:
                    raise CoverSaveError("另一个保存或恢复操作正在占用此文件夹，请稍后重试。")
                time.sleep(.025)
        try:
            yield
        finally:
            # No data mutation follows unlink; contenders verify the inode after locking.
            if lock_path.exists() and not _is_reparse(lock_path) and os.fstat(stream.fileno()).st_ino == lock_path.stat().st_ino:
                lock_path.unlink()
            stream.close()


def _write_journal(transaction, document):
    pending = transaction / "journal.pending"
    with pending.open("w", encoding="utf-8") as writer:
        json.dump(document, writer, ensure_ascii=False)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(pending, transaction / "journal.json")


def _record(fingerprint):
    return dict(sha256=fingerprint.sha256, identity=list(fingerprint.stat_key[:2]))


def _matches(path, record):
    if not record or not path.exists() or _is_reparse(path):
        return False
    value = FileFingerprint.capture(path)
    return value.sha256 == record['sha256'] and list(value.stat_key[:2]) == record['identity']


def _read_journal(transaction, parent):
    journal = transaction / "journal.json"
    if (_is_reparse(transaction.parent) or _is_reparse(transaction) or _is_reparse(journal)
            or not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{32}', transaction.name)
            or journal.stat().st_size > 65536):
        raise CoverSaveError("保存恢复记录不安全，已保留临时文件，未覆盖任何文件。")
    document = json.loads(journal.read_text('utf-8'))
    if (not isinstance(document, dict) or document.get('schema') not in (
            'liveviewer.cover-pair-transaction.v1', 'liveviewer.cover-pair-transaction.v2')
            or document.get('transaction') != transaction.name
            or document.get('parent') != str(parent.resolve())
            or document.get('mode') not in ('copy', 'overwrite')
            or document.get('state') not in ('staging', 'prepared', 'committed')):
        raise CoverSaveError("保存恢复记录无效，已保留临时文件，未覆盖任何文件。")
    version_two = document['schema'].endswith('.v2')
    entries = document.get('entries', [])
    source_names = document.get('source_names')
    if (not isinstance(source_names, list) or len(source_names) != 2
            or not all(_safe_filename(name) for name in source_names)
            or Path(source_names[0]).suffix.casefold() not in ('.jpg', '.jpeg', '.heic')
            or Path(source_names[1]).suffix.casefold() not in ('.mov', '.mp4')
            or Path(source_names[0]).stem.casefold() != Path(source_names[1]).stem.casefold()):
        raise CoverSaveError("保存恢复的原组名称无效，未执行恢复。")
    if not isinstance(entries, list) or len(entries) > 2 or document['state'] != 'staging' and len(entries) != 2:
        raise CoverSaveError("保存恢复记录缺少完整的图片和视频，未执行恢复。")
    for index, row in enumerate(entries):
        if not isinstance(row, dict):
            raise CoverSaveError("保存恢复条目无效，未执行恢复。")
        name = row.get('target', '')
        if not _safe_filename(name):
            raise CoverSaveError("保存恢复目标越界，未执行恢复。")
        source_name = row.get('source') if version_two else source_names[index]
        if source_name != source_names[index]:
            raise CoverSaveError("保存恢复原组路径不一致，未执行恢复。")
        converted = (version_two and document['mode'] == 'overwrite' and index == 0
                     and Path(source_name).suffix.casefold() in ('.jpg', '.jpeg'))
        expected_target = str(Path(source_name).with_suffix('.HEIC')) if converted else source_name
        if document['mode'] == 'overwrite' and name != expected_target:
            raise CoverSaveError("恢复目标不属于此原组，未执行恢复。")
        if document['mode'] == 'copy':
            suffix = '.HEIC' if version_two and index == 0 else Path(source_names[index]).suffix
            base = Path(source_names[index]).stem
            if Path(name).suffix != suffix or not re.fullmatch(re.escape(base) + r' v[1-9][0-9]*', Path(name).stem):
                raise CoverSaveError("恢复副本名称不属于此原组，未执行恢复。")
        target, source_path = parent / name, parent / source_name
        if (_is_reparse(target) or _is_reparse(source_path)
                or target.parent.resolve() != parent.resolve()):
            raise CoverSaveError("保存恢复目标是链接或越界，未执行恢复。")
        expected_backup = ('original-image', 'original-video')[index] if document['mode'] == 'overwrite' else None
        suffixes = (('.jpg', '.jpeg', '.heic'), ('.mov', '.mp4'))[index]
        expected_stage = 'cover.HEIC' if version_two and index == 0 else ('cover', 'video')[index] + Path(name).suffix
        if Path(name).suffix.casefold() not in suffixes or row.get('staged') != expected_stage:
            raise CoverSaveError("保存恢复文件角色无效，未执行恢复。")
        if row.get('backup') != expected_backup:
            raise CoverSaveError("保存恢复原件路径无效，未执行恢复。")
        if version_two and index == 0 and Path(name).suffix.casefold() != '.heic':
            raise CoverSaveError("保存恢复新图片不是 HEIC，未执行恢复。")
        for key in ('old', 'new', 'backup_identity'):
            record = row.get(key)
            if record is None and (key in ('old', 'new') or document['mode'] == 'overwrite'):
                raise CoverSaveError("保存恢复缺少所有权指纹，未执行恢复。")
            if record is not None and (not isinstance(record, dict)
                    or not isinstance(record.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                    or not isinstance(record.get('identity'), list) or len(record['identity']) != 2
                    or any(type(number) is not int for number in record['identity'])):
                raise CoverSaveError("保存恢复指纹无效，未执行恢复。")
    if len(entries) == 2 and (entries[0]['target'].casefold() == entries[1]['target'].casefold()
            or Path(entries[0]['target']).stem.casefold() != Path(entries[1]['target']).stem.casefold()):
        raise CoverSaveError("恢复图片和视频不是同名的独立文件，未执行恢复。")
    staging_records = document.get('staging_records', {})
    staging_names = {'cover.HEIC', 'video' + Path(source_names[1]).suffix}
    if document['mode'] == 'overwrite':
        staging_names.update(('original-image', 'original-video'))
    if not isinstance(staging_records, dict) or len(staging_records) > 4:
        raise CoverSaveError("暂存文件所有权记录无效，未执行恢复。")
    for name, record in staging_records.items():
        if (not version_two or name not in staging_names or not isinstance(record, dict)
                or not isinstance(record.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                or not isinstance(record.get('identity'), list) or len(record['identity']) != 2
                or any(type(number) is not int for number in record['identity'])):
            raise CoverSaveError("暂存文件所有权指纹无效，未执行恢复。")
    retired = document.get('retired_images', [])
    if (not isinstance(retired, list) or len(retired) > 1000
            or (not version_two or document['mode'] == 'copy') and retired):
        raise CoverSaveError("保存恢复图片清理列表无效，未执行恢复。")
    names = set()
    for index, row in enumerate(retired):
        if not isinstance(row, dict):
            raise CoverSaveError("保存恢复图片清理条目无效，未执行恢复。")
        name, record = row.get('name'), row.get('old')
        if (not _safe_filename(name) or Path(name).suffix.casefold() not in ('.jpg', '.jpeg')
                or Path(name).stem.casefold() != Path(source_names[0]).stem.casefold()
                or name.casefold() in names or row.get('retired') != f'retired-image-{index}'
                or _is_reparse(parent / name) or not isinstance(record, dict)
                or not isinstance(record.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                or not isinstance(record.get('identity'), list) or len(record['identity']) != 2
                or any(type(number) is not int for number in record['identity'])):
            raise CoverSaveError("保存恢复图片清理范围或指纹无效，未执行恢复。")
        names.add(name.casefold())
    if (version_two and document['mode'] == 'overwrite' and document['state'] != 'staging'
            and Path(source_names[0]).suffix.casefold() in ('.jpg', '.jpeg')):
        original = next((row for row in retired if row['name'] == source_names[0]), None)
        if original is None or original['old'] != entries[0]['old']:
            raise CoverSaveError("保存恢复缺少原 JPG 图片清理记录，未执行恢复。")
    reservation = document.get('reservation')
    if reservation is not None:
        if (not isinstance(reservation, dict) or not re.fullmatch(r'\.reserve-[0-9a-f]{64}', reservation.get('name', ''))):
            raise CoverSaveError("副本预留记录无效，未执行恢复。")
    return document


def _remove_transaction(transaction, document):
    if _is_reparse(transaction.parent) or _is_reparse(transaction):
        raise CoverSaveError("保存临时目录已变成链接，保留资料且不执行清理。")
    allowed = {'journal.json', 'journal.pending', 'original-image', 'original-video',
               'cover.jpg', 'cover.jpeg', 'cover.heic', 'video.mov', 'video.mp4'}
    # Preserve unexpected material and links rather than following or deleting it.
    paths = list(transaction.iterdir())
    identities = {}
    owned_records = {name.casefold(): record for name, record in document.get('staging_records', {}).items()}
    for row in document.get('entries', []):
        owned_records[row['staged'].casefold()] = row['new']
        if row.get('backup'):
            owned_records[row['backup'].casefold()] = row['backup_identity']
    for row in document.get('retired_images', []):
        allowed.add(row['retired'])
        owned_records[row['retired'].casefold()] = row['old']
    seen = set()
    for path in paths:
        folded = path.name.casefold()
        if (folded not in allowed or folded in seen or _is_reparse(path) or not path.is_file()
                or folded not in ('journal.json', 'journal.pending') and folded not in owned_records):
            raise CoverSaveError("临时目录含未知文件，已保留供检查，未删除该目录。")
        seen.add(folded)
        identities[path.name] = _stat_key(path.stat())
        if folded in owned_records and not _matches(path, owned_records[folded]):
            raise CoverSaveError("临时恢复文件被其他程序修改，已保留供检查，未删除该文件。")
    reservation = document.get('reservation')
    if reservation:
        path = transaction.parent / reservation['name']
        if _matches(path, reservation.get('fingerprint')):
            path.unlink()
    # Check again after the potentially long ownership hashes and before each
    # removal; never traverse a replacement directory/link during cleanup.
    paths.sort(key=lambda path: path.name.casefold() == 'journal.json')
    for path in paths:
        if (_is_reparse(transaction.parent) or _is_reparse(transaction) or _is_reparse(path)
                or _stat_key(path.stat()) != identities[path.name]):
            raise CoverSaveError("清理期间恢复文件发生变化，已保留其余恢复资料。")
        path.unlink()
    transaction.rmdir()


def _unlink_owned(path, record):
    current = FileFingerprint.capture(path)
    if (_record(current) != record or _is_reparse(path)
            or _stat_key(path.stat()) != current.stat_key):
        raise SourceChangedError("待撤销输出已被其他程序修改，已保留该文件。")
    path.unlink()


def _verify_published_pair(parent, entries):
    identities = {row['target']: _stat_key((parent / row['target']).stat()) for row in entries}
    if (not all(_matches(parent / row['target'], row['new']) for row in entries)
            or any(_is_reparse(parent / row['target'])
                   or _stat_key((parent / row['target']).stat()) != identities[row['target']]
                   for row in entries)):
        raise SourceChangedError("提交期间输出被其他程序修改，正在恢复本事务拥有的文件。")
    return identities


def _remember_stage(transaction, document, path, expected_sha256, cancel_event):
    current = FileFingerprint.capture(path, cancel_event)
    if current.sha256 != expected_sha256:
        raise SourceChangedError("暂存文件被其他程序修改，已保留而未发布。")
    document['staging_records'][path.name] = _record(current)
    _write_journal(transaction, document)


def _recover_retired_images(transaction, parent, rows):
    conflicts = []
    for row in reversed(rows):
        source, retired = parent / row['name'], transaction / row['retired']
        if _matches(source, row['old']):
            continue
        if source.exists() or _is_reparse(source) or not _matches(retired, row['old']):
            conflicts.append(source.name)
            continue
        current = FileFingerprint.capture(retired)
        if (_record(current) != row['old'] or _is_reparse(retired)
                or _stat_key(retired.stat()) != current.stat_key):
            conflicts.append(source.name)
            continue
        # Never replace a newly created file at the old JPG path.
        _publish_no_replace(retired, source)
    return conflicts


def _recover_transaction(transaction, parent):
    document = _read_journal(transaction, parent)
    if document['state'] == 'staging':
        _remove_transaction(transaction, document)
        return
    entries = document['entries']
    if document['state'] == 'committed':
        _verify_published_pair(parent, entries)
        if any((parent / row['name']).exists() or _is_reparse(parent / row['name'])
               for row in document.get('retired_images', [])):
            raise CoverSaveError("保存完成后原 JPG 位置出现其他文件，恢复资料已保留。")
        _remove_transaction(transaction, document)
        return
    conflicts = _recover_retired_images(transaction, parent, document.get('retired_images', []))
    for row in reversed(entries):
        target = parent / row['target']
        converted = (document['schema'].endswith('.v2') and document['mode'] == 'overwrite'
                     and row['source'] != row['target'])
        if document['mode'] == 'copy' or converted:
            if _matches(target, row['new']):
                _unlink_owned(target, row['new'])
            elif (target.exists() or _is_reparse(target)) and not _matches(transaction / row['staged'], row['new']):
                conflicts.append(target.name)
            continue
        if _matches(target, row.get('old')) or _matches(target, row.get('backup_identity')):
            continue
        if not _matches(target, row['new']):
            conflicts.append(target.name)
            continue
        backup = transaction / row['backup']
        backup_identity = _stat_key(backup.stat()) if backup.exists() else None
        if not _matches(backup, row.get('backup_identity')):
            conflicts.append(target.name)
            continue
        # Backup verification may hash a large MOV. Revalidate ownership after
        # that work, then check both identities immediately before replacement.
        current = FileFingerprint.capture(target)
        if (_record(current) != row['new'] or _is_reparse(target) or _is_reparse(backup)
                or _stat_key(target.stat()) != current.stat_key
                or _stat_key(backup.stat()) != backup_identity):
            conflicts.append(target.name)
            continue
        os.replace(backup, target)
    if conflicts:
        if document['mode'] == 'copy':
            _remove_transaction(transaction, document)
            raise CoverSaveError("副本保存未完成；已保留外部修改的文件：" + "、".join(conflicts)
                                 + "。本次保存创建且仍归本事务所有的副本已撤销，请检查保留的文件。")
        raise CoverSaveError("保存未完成，文件被占用或被其他程序修改，已保留：" + "、".join(conflicts)
                             + "。请勿删除编辑临时目录；重新打开前先检查恢复资料。")
    _remove_transaction(transaction, document)


def _recover_pending_locked(root, parent):
    for transaction in root.iterdir():
        if transaction.name.startswith('.') or not transaction.is_dir():
            continue
        if _is_reparse(transaction):
            raise CoverSaveError("保存临时目录包含符号链接，未执行恢复。")
        if (transaction / 'journal.json').exists():
            _recover_transaction(transaction, parent)


def recover_cover_transactions(parent: Path, *, cancel_event=None) -> None:
    parent = Path(parent)
    root = parent / WORK_DIRECTORY
    if not root.exists() and not _is_reparse(root):
        return
    root = _work_root(parent)
    with _parent_transaction_lock(root, cancel_event):
        _recover_pending_locked(root, parent)


def save_cover(source: CoverSource, image: Image.Image, mode: str = "copy", *,
               photo_time: Fraction | None = None,
               cancel_event: threading.Event | None = None,
               heic_encoder: Callable | None = None) -> SaveResult:
    """Stage, validate and commit both resources, with recoverable pair rollback."""
    if mode not in ("copy", "overwrite"):
        raise ValueError("Unknown cover save mode")
    if not isinstance(photo_time, Fraction) or photo_time < 0:
        raise CoverSaveError("缺少所选帧的真实时间戳，未保存；请重新读取视频后选择封面图。")
    if image.width < 1 or image.height < 1:
        raise CoverSaveError("所选视频帧为空，无法保存。")
    _cancelled(cancel_event)
    source.validate(cancel_event)
    parent = source.image.path.parent
    _writable(parent)
    if mode == "overwrite":
        _writable(source.image.path)
        _writable(source.video.path)
    root = _work_root(parent)
    with _parent_transaction_lock(root, cancel_event):
        _recover_pending_locked(root, parent)
        source.validate(cancel_event)
        return _save_locked(source, image, mode, photo_time, root, cancel_event, heic_encoder)


def _save_locked(source, image, mode, photo_time, root, cancel_event, heic_encoder):
    from .live_photo_image import read_content_identifier, validate_cover_metadata
    from .live_photo_movie import inspect_live_photo_movie, verify_movie_payload_preserved
    from .live_photo_native_image import write_cover, set_movie_identifier
    from .live_photo_native_movie import rewrite_original_marker
    parent = source.image.path.parent
    identifier = str(uuid.uuid4())
    target_image = (source.image.path if source.image.path.suffix.casefold() == '.heic'
                    else source.image.path.with_suffix('.HEIC'))
    target_video = source.video.path
    retiring = []
    if mode == 'overwrite':
        for path in _same_stem_files(parent, source.image.path.stem):
            if path.suffix.casefold() == '.heic' and path != source.image.path:
                raise FileExistsError("同名 HEIC 已存在，未覆盖或删除任何文件。")
            if path.suffix.casefold() in ('.jpg', '.jpeg'):
                _writable(path)
                retiring.append(FileFingerprint.capture(path, cancel_event))
        retiring.sort(key=lambda value: value.path.name.casefold())
        if len(retiring) > 1000:
            raise CoverSaveError("同名 JPG/JPEG 文件过多，未执行覆盖。")
    transaction = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex)
    transaction.mkdir()
    document = dict(schema='liveviewer.cover-pair-transaction.v2', transaction=transaction.name,
                    parent=str(parent.resolve()), mode=mode, state='staging', entries=[], reservation=None,
                    staging_records={},
                    source_names=[source.image.path.name, source.video.path.name],
                    retired_images=[dict(name=value.path.name, retired=f'retired-image-{index}', old=_record(value))
                                    for index, value in enumerate(retiring)])
    journal_written = False
    try:
        _write_journal(transaction, document)
        journal_written = True
        staged_image = transaction / 'cover.HEIC'
        staged_video = transaction / ('video' + source.video.path.suffix)
        image_report = write_cover(source.image.path, image, staged_image, identifier, 'HEIC', cancel_event,
                    heic_encoder=lambda pixels, target, *, cancel_event=None:
                        _encode_image(pixels, target, cancel_event, heic_encoder))
        _remember_stage(transaction, document, staged_image, image_report['output_sha256'], cancel_event)
        source.validate(cancel_event)
        marker_report = rewrite_original_marker(source.video.path, staged_video, photo_time, cancel_event)
        _remember_stage(transaction, document, staged_video, marker_report['output_sha256'], cancel_event)
        verify_movie_payload_preserved(source.video.path, staged_video, cancel_event=cancel_event)
        # ExifTool may relocate chunk offsets. Its ID-only edit independently
        # checks every sample byte/time and every other track structure field.
        movie_report = set_movie_identifier(staged_video, identifier, cancel_event)
        _remember_stage(transaction, document, staged_video, movie_report['output_sha256'], cancel_event)
        validate_cover_metadata(staged_image, identifier)
        written_movie = inspect_live_photo_movie(staged_video, cancel_event=cancel_event)
        if (read_content_identifier(staged_image) != identifier
                or not written_movie.supported or written_movie.asset_identifier != identifier
                or written_movie.photo_time != photo_time or written_movie.marker_count != 1):
            raise CoverSaveError("保存后的图片与视频配对或封面时间校验失败，原文件未更改。")
        if mode == 'copy':
            target_image, target_video, reservation = _reserve_copy(source, root)
            assert reservation is None
        for staged, target, original, backup_name in (
                (staged_image, target_image, source.image, 'original-image'),
                (staged_video, target_video, source.video, 'original-video')):
            backup_record = None
            if mode == 'overwrite':
                backup = transaction / backup_name
                _copy_checked(original, backup, cancel_event)
                _remember_stage(transaction, document, backup, original.sha256, cancel_event)
                backup_record = _record(FileFingerprint.capture(backup, cancel_event))
            document['entries'].append(dict(source=original.path.name, target=target.name, staged=staged.name, new=_record(FileFingerprint.capture(staged, cancel_event)),
                                            old=_record(original), backup=backup_name if mode == 'overwrite' else None,
                                            backup_identity=backup_record))
        source.validate(cancel_event)
        for value in retiring:
            value.validate(cancel_event)
        _cancelled(cancel_event)
        source.validate_identity()
        if mode == 'copy' and _same_stem_files(parent, target_image.stem):
            raise FileExistsError("副本文件名已被其他程序使用，请重新保存。")
        document['state'] = 'prepared'
        _write_journal(transaction, document)
        source.validate_identity()
        if mode == 'overwrite' and target_image == source.image.path:
            os.replace(staged_image, target_image)
        else:
            _publish_no_replace(staged_image, target_image)
        _cancelled(cancel_event)
        if mode == 'copy' and any(path != target_image for path in _same_stem_files(parent, target_image.stem)):
            raise FileExistsError("副本文件名已被其他程序使用，请重新保存。")
        if mode == 'overwrite':
            # Recheck the second original after the first replace; never clobber
            # another process's edit made between the two publications.
            source.video.validate(cancel_event)
            os.replace(staged_video, target_video)
        else:
            _publish_no_replace(staged_video, target_video)
        identities = _verify_published_pair(parent, document['entries'])
        _cancelled(cancel_event)
        for value, row in zip(retiring, document['retired_images']):
            value.validate(cancel_event)
            if (any(_is_reparse(parent / entry['target'])
                    or _stat_key((parent / entry['target']).stat()) != identities[entry['target']]
                    for entry in document['entries'])
                    or _stat_key(value.path.stat()) != value.stat_key or _is_reparse(value.path)):
                raise SourceChangedError("清理原 JPG 前文件已变化，正在恢复本次保存。")
            _publish_no_replace(value.path, transaction / row['retired'])
            _cancelled(cancel_event)
        _verify_published_pair(parent, document['entries'])
        if mode == 'overwrite' and any(path.suffix.casefold() in ('.jpg', '.jpeg')
                                      for path in _same_stem_files(parent, target_image.stem)):
            raise SourceChangedError("提交期间出现新的同名 JPG/JPEG，已保留外部文件并取消覆盖。")
        _cancelled(cancel_event)
        document['state'] = 'committed'
        _write_journal(transaction, document)
        _remove_transaction(transaction, document)
        return SaveResult(target_image, target_video, mode, None, source.image.path,
                          source.video.path, identifier, photo_time)
    except BaseException:
        reservation_record = document.get('reservation')
        try:
            if journal_written and (transaction / 'journal.json').exists():
                _recover_transaction(transaction, parent)
            elif transaction.exists():
                _remove_transaction(transaction, document)
        except Exception as recovery_error:
            if transaction.exists():
                detail = "已保留恢复资料：" + str(transaction) + "。"
            else:
                detail = "外部修改的文件已保留。"
            raise CoverSaveError("保存未完成，自动恢复未完成或遇到外部修改；"
                                 + detail + str(recovery_error)) from recovery_error
        if reservation_record:
            path = root / reservation_record['name']
            if _matches(path, reservation_record['fingerprint']):
                path.unlink()
        raise
