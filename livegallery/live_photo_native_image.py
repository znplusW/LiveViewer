"""Preserve complete Apple metadata while writing a new Live Photo resource.

Apple MakerNotes must already exist: ExifTool copies the complete block, then
edits ContentIdentifier in a second invocation. Every other typed field is
compared byte-for-byte; legal offset/alignment changes are reported separately.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time

from PIL import Image
from . import imaging as _imaging
from .heic_encoder import encode_heic
from . import live_photo_movie as movie

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
          10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
_CAPTURE = ('IFD0:Make', 'IFD0:Model', 'ExifIFD:DateTimeOriginal', 'ExifIFD:CreateDate',
            'ExifIFD:OffsetTimeOriginal', 'ExifIFD:OffsetTimeDigitized',
            'ExifIFD:SubSecTimeOriginal', 'ExifIFD:SubSecTimeDigitized', 'GPS:all')
_CAPTURE_IFD0 = {0x10f: 'Make', 0x110: 'Model'}
_CAPTURE_EXIF = {0x9003: 'DateTimeOriginal', 0x9004: 'CreateDate',
                 0x9011: 'OffsetTimeOriginal', 0x9012: 'OffsetTimeDigitized',
                 0x9291: 'SubSecTimeOriginal', 0x9292: 'SubSecTimeDigitized'}
_GPS_NAMES = ('GPSVersionID', 'GPSLatitudeRef', 'GPSLatitude', 'GPSLongitudeRef',
    'GPSLongitude', 'GPSAltitudeRef', 'GPSAltitude', 'GPSTimeStamp', 'GPSSatellites',
    'GPSStatus', 'GPSMeasureMode', 'GPSDOP', 'GPSSpeedRef', 'GPSSpeed', 'GPSTrackRef',
    'GPSTrack', 'GPSImgDirectionRef', 'GPSImgDirection', 'GPSMapDatum',
    'GPSDestLatitudeRef', 'GPSDestLatitude', 'GPSDestLongitudeRef', 'GPSDestLongitude',
    'GPSDestBearingRef', 'GPSDestBearing', 'GPSDestDistanceRef', 'GPSDestDistance',
    'GPSProcessingMethod', 'GPSAreaInformation', 'GPSDateStamp', 'GPSDifferential', 'GPSHPositioningError')


def _cancel(event):
    if event is not None and event.is_set():
        raise InterruptedError('已取消。')


def _tool():
    if getattr(sys, 'frozen', False):
        path = Path(sys._MEIPASS) / 'tools/exiftool/exiftool.exe'
    else:
        path = Path(__file__).resolve().parents[1] / 'vendor/exiftool/exiftool.exe'
    if not path.is_file():
        raise OSError('ExifTool 组件缺失，请重新完整解压 LiveViewer。')
    return path


def _run(arguments, cancel_event=None):
    _cancel(cancel_event)
    arguments = [str(value) for value in arguments]
    if any(any(character in value for character in ('\r', '\n', '\0')) for value in arguments):
        raise ValueError('ExifTool 参数不能包含换行或 NUL 字符。')
    argument_bytes = ('\n'.join(arguments) + '\n').encode('utf8')
    env = os.environ.copy()
    # The bundled Windows Perl does not provide the POSIX C.UTF-8 locale.
    env.update(LC_ALL='C', LC_CTYPE='C', LANG='C')
    # Windows Perl's command-line decoding loses characters outside the ACP.
    # Keep its bootstrap arguments ASCII and send actual arguments as UTF-8.
    process = subprocess.Popen([str(_tool()), '-charset', 'filename=UTF8', '-@', '-'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    deadline = time.monotonic() + 120
    first_communication = True
    try:
        while True:
            _cancel(cancel_event)
            if time.monotonic() > deadline:
                raise TimeoutError('ExifTool 元数据处理超时。')
            try:
                stdout, stderr = process.communicate(
                    input=argument_bytes if first_communication else None, timeout=.1)
                break
            except subprocess.TimeoutExpired:
                first_communication = False
        _cancel(cancel_event)
        if process.returncode:
            raise OSError('ExifTool 处理失败：' + stderr.decode('utf8', 'replace')[-2000:])
        if len(stdout) > 4 * 1024 * 1024:
            raise ValueError('元数据超过支持的读取范围。')
        # No -m / ignore-minor-errors: corrupt MakerNotes must fail closed.
        if stderr.strip():
            raise ValueError('ExifTool 未能无警告完成：' + stderr.decode('utf8', 'replace')[-2000:])
        return stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _path(path, *, existing=True):
    path = Path(os.path.abspath(path))
    for parent in ((path,) if path.exists() else ()) + tuple(path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('资源暂不支持链接或重解析路径。')
    if existing and not path.is_file():
        raise ValueError('源资源不存在。')
    return path


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value) is None:
        raise ValueError('新配对标识必须是 36 字符 UUID。')
    return value


def _signature(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _sha(path, event=None):
    state = _signature(path)
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            _cancel(event)
            value.update(block)
    _cancel(event)
    if _signature(path) != state:
        raise OSError('源资源在读取期间发生变化。')
    return value.hexdigest()


def _same(path, state, digest, event):
    if _signature(path) != state or _sha(path, event) != digest or _signature(path) != state:
        raise OSError('源资源已变化，保存已停止。')


def _note(data):
    if not 20 <= len(data) <= 1024 * 1024 or data[:10] != b'Apple iOS\0' or data[12:14] not in (b'MM', b'II'):
        raise ValueError('源图片没有受支持的完整 Apple MakerNotes。')
    endian = '>' if data[12:14] == b'MM' else '<'
    count = struct.unpack_from(endian + 'H', data, 14)[0]
    end = 16 + 12 * count
    if not 1 <= count <= 4096 or end + 4 > len(data) or struct.unpack_from(endian + 'I', data, end)[0]:
        raise ValueError('Apple MakerNotes 目录长度或后续 IFD 不受支持。')
    fields, locations = {}, {}
    for offset in range(16, end, 12):
        tag, kind, amount, pointer = struct.unpack_from(endian + 'HHII', data, offset)
        if tag in fields or kind not in _SIZES or amount > 1024 * 1024:
            raise ValueError('Apple MakerNotes 含重复或不支持的字段。')
        size = amount * _SIZES[kind]
        start = offset + 8 if size <= 4 else pointer
        if (size > 4 and start < end + 4) or start + size > len(data):
            raise ValueError('Apple MakerNotes 字段引用越界。')
        fields[tag] = (kind, amount, data[start:start + size])
        locations[tag] = (start, size)
    if 17 not in fields or fields[17][0] != 2 or not fields[17][2].endswith(b'\0'):
        raise ValueError('源 Apple ContentIdentifier 缺失或格式不受支持。')
    return data[:14], fields, locations


def _read_exif(path, event):
    _cancel(event)
    with Image.open(path) as image:
        if image.format not in ('JPEG', 'HEIF'):
            raise ValueError('源图片仅支持 JPEG 或 HEIC。')
        raw = image.info.get('exif', b'')
        if image.format == 'HEIF':
            from pi_heif import open_heif
            raw = open_heif(path).info.get('exif') or b''
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
        raise ValueError('源图片 EXIF 缺失或超出读取范围。')
    if not raw:
        raise ValueError('源图片 EXIF 缺失。')
    _cancel(event)
    return raw


def _read_note(path, event):
    exif = Image.Exif()
    exif.load(_read_exif(path, event))
    data = exif.get_ifd(0x8769).get(0x927c, b'')
    if not isinstance(data, bytes):
        raise ValueError('Apple MakerNotes 原始字节无效。')
    _cancel(event)
    return data, _note(data)


def _capture_raw(path, event):
    """Read only bounded classic TIFF capture fields, without numeric conversion."""
    raw = _read_exif(path, event)
    data = raw[6:] if raw.startswith(b'Exif\0\0') else raw
    if len(data) < 8 or data[:2] not in (b'II', b'MM'):
        raise ValueError('不支持此 EXIF TIFF 字节序或头。')
    endian = '<' if data[:2] == b'II' else '>'
    if struct.unpack_from(endian + 'H', data, 2)[0] != 42:
        raise ValueError('仅支持标准 TIFF EXIF，不支持 BigTIFF。')
    seen, directory_ranges, value_ranges = set(), [], []
    payload_total = 0

    def directory(start, group):
        nonlocal payload_total
        if start in seen or not 8 <= start <= len(data) - 2:
            raise ValueError(f'{group} EXIF 目录引用重复、循环或越界。')
        seen.add(start)
        count = struct.unpack_from(endian + 'H', data, start)[0]
        end = start + 2 + count * 12
        if count > 4096 or end + 4 > len(data):
            raise ValueError(f'{group} EXIF 目录长度无效。')
        if any(start < stop and end + 4 > begin for begin, stop in directory_ranges):
            raise ValueError(f'{group} EXIF 目录范围重叠。')
        directory_ranges.append((start, end + 4))
        fields = {}
        for offset in range(start + 2, end, 12):
            tag, kind, amount, pointer = struct.unpack_from(endian + 'HHII', data, offset)
            if tag in fields or kind not in _SIZES or amount > 1024 * 1024:
                raise ValueError(f'{group}:0x{tag:04x} 字段重复、类型或数量不受支持。')
            size = amount * _SIZES[kind]
            payload_total += size
            if payload_total > 4 * 1024 * 1024:
                raise ValueError('EXIF 字段累计读取长度超过支持范围。')
            location = offset + 8 if size <= 4 else pointer
            if location < 8 or location + size > len(data):
                raise ValueError(f'{group}:0x{tag:04x} 字段引用越界。')
            if size > 4:
                value_ranges.append((location, location + size, group, tag))
            fields[tag] = (kind, amount, data[location:location + size])
        next_ifd = struct.unpack_from(endian + 'I', data, end)[0]
        if group != 'IFD0' and next_ifd:
            raise ValueError(f'{group} 后续 EXIF 目录不受支持。')
        return fields

    root = directory(struct.unpack_from(endian + 'I', data, 4)[0], 'IFD0')

    def child(tag, group):
        if tag not in root:
            return {}
        kind, count, payload = root[tag]
        if kind != 4 or count != 1:
            raise ValueError(f'{group} EXIF 目录指针类型无效。')
        return directory(struct.unpack(endian + 'I', payload)[0], group)

    exif, gps = child(0x8769, 'ExifIFD'), child(0x8825, 'GPS')
    for start, end, group, tag in value_ranges:
        if any(start < stop and end > begin for begin, stop in directory_ranges):
            raise ValueError(f'{group}:0x{tag:04x} 字段覆盖 EXIF 目录。')
    for tag, (kind, count, payload) in gps.items():
        if kind in (13, 18):
            raise ValueError(f'GPS:0x{tag:04x} 指针类型不支持精确复制。')
        if kind in (5, 10):
            code = 'I' if kind == 5 else 'i'
            if any(struct.unpack_from(endian + code, payload, n + 4)[0] == 0
                   for n in range(0, len(payload), 8)):
                raise ValueError(f'GPS:0x{tag:04x} 有理数分母为零。')
    selected = {'IFD0': {tag: root[tag] for tag in _CAPTURE_IFD0 if tag in root},
                'ExifIFD': {tag: exif[tag] for tag in _CAPTURE_EXIF if tag in exif}, 'GPS': gps}
    for group in ('IFD0', 'ExifIFD'):
        for tag, (kind, count, payload) in selected[group].items():
            if kind != 2 or not count or not payload.endswith(b'\0'):
                raise ValueError(f'{group}:0x{tag:04x} 拍摄文字字段类型无效。')
    return {'endian': endian, **selected}


def _capture_block(capture, note):
    """Build a minimal EXIF block, preserving selected typed values exactly."""
    endian = capture['endian']
    groups = {group: dict(capture[group]) for group in ('IFD0', 'ExifIFD', 'GPS')}
    groups['ExifIFD'][0x927c] = (7, len(note), note)
    groups['IFD0'][0x8769] = (4, 1, b'\0' * 4)
    if groups['GPS']:
        groups['IFD0'][0x8825] = (4, 1, b'\0' * 4)
    order = ['IFD0', 'ExifIFD'] + (['GPS'] if groups['GPS'] else [])
    offsets, position = {}, 8
    for group in order:
        offsets[group] = position
        position += 2 + len(groups[group]) * 12 + 4
    groups['IFD0'][0x8769] = (4, 1, struct.pack(endian + 'I', offsets['ExifIFD']))
    if groups['GPS']:
        groups['IFD0'][0x8825] = (4, 1, struct.pack(endian + 'I', offsets['GPS']))
    total_size = position
    for group in order:
        for kind, count, payload in groups[group].values():
            if kind not in _SIZES or len(payload) != count * _SIZES[kind]:
                raise ValueError('EXIF 字段长度不一致。')
            if len(payload) > 4:
                total_size += total_size % 2 + len(payload)
            if total_size > 65527:
                raise ValueError('精确 EXIF 块超过支持可写入 JPEG APP1 的范围。')
    content = bytearray((b'II' if endian == '<' else b'MM') + struct.pack(endian + 'HI', 42, 8))
    content += b'\0' * (position - len(content))
    for group in order:
        offset = offsets[group]
        struct.pack_into(endian + 'H', content, offset, len(groups[group]))
        for index, (tag, (kind, count, payload)) in enumerate(sorted(groups[group].items())):
            if len(payload) != count * _SIZES[kind]:
                raise ValueError('EXIF 字段长度不一致。')
            if len(payload) <= 4:
                value = payload.ljust(4, b'\0')
            else:
                if len(content) % 2:
                    content += b'\0'
                value = struct.pack(endian + 'I', len(content))
                content += payload
            entry = offset + 2 + index * 12
            content[entry:entry + 12] = struct.pack(endian + 'HHI', tag, kind, count) + value
    if len(content) > 65527:
        raise ValueError('精确 EXIF 块超过支持可写入 JPEG APP1 的范围。')
    return bytes(content)


def _capture_name(group, tag):
    names = _CAPTURE_IFD0 if group == 'IFD0' else _CAPTURE_EXIF if group == 'ExifIFD' else dict(enumerate(_GPS_NAMES))
    return f'{group}:{names.get(tag, f"0x{tag:04x}")}'


def _verify_capture_raw(before, after):
    if before['endian'] != after['endian']:
        raise ValueError('EXIF 原始字节序未保留。')
    changes = []
    for group in ('IFD0', 'ExifIFD', 'GPS'):
        old, new = before[group], after[group]
        for tag in sorted(old.keys() | new.keys()):
            if old.get(tag) != new.get(tag):
                changes.append(_capture_name(group, tag))
    if changes:
        raise ValueError('拍摄元信息未精确保留：' + '、'.join(changes))


def _capture(path, event):
    import json
    result = json.loads(_run(['-j', '-a', '-G1', '-s', '-n', *('-' + key for key in _CAPTURE), path], event))[0]
    return {key: value for key, value in result.items() if key != 'SourceFile'}


def _verify_note(before, after, identifier):
    original_raw, (original_head, original_fields, original_locations) = before
    output_raw, (output_head, output_fields, output_locations) = after
    if original_head != output_head or original_fields.keys() != output_fields.keys():
        raise ValueError('ExifTool 改变了 Apple MakerNotes 前缀或字段集合。')
    if any(original_fields[key] != output_fields[key] for key in original_fields if key != 17):
        raise ValueError('ExifTool 改变了非配对标识的 MakerNote 字段，保存已停止。')
    wanted = identifier.encode('ascii') + b'\0'
    if output_fields[17] != (2, len(wanted), wanted):
        raise ValueError('新 Apple ContentIdentifier 未写入或读回不一致。')
    original_mask, output_mask = bytearray(original_raw), bytearray(output_raw)
    for content, locations in ((original_mask, original_locations), (output_mask, output_locations)):
        start, size = locations[17]
        content[start:start + size] = bytes(size)
    return {'typed_all_nonid_values_equal': True,
            'physical_equal_except_identifier': original_mask == output_mask,
            'source_makernote_size': len(original_raw), 'output_makernote_size': len(output_raw),
            'source_makernote_sha256': hashlib.sha256(original_raw).hexdigest(),
            'output_makernote_sha256': hashlib.sha256(output_raw).hexdigest(),
            'preserved_nonid_fields': [{'tag': tag, 'type': item[0], 'count': item[1],
                'value_sha256': hashlib.sha256(item[2]).hexdigest()}
                for tag, item in original_fields.items() if tag != 17]}


def _copy_exclusive(source, destination, event):
    created = False
    try:
        with destination.open('xb') as target:
            created = True
            with source.open('rb') as original:
                while block := original.read(1024 * 1024):
                    _cancel(event)
                    target.write(block)
            target.flush()
        _cancel(event)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def write_cover(source_image, pixels, destination, identifier, image_format, cancel_event=None, *,
                heic_encoder=None):
    identifier = _identifier(identifier)
    source, destination = _path(source_image), _path(destination, existing=False)
    if destination.exists() or source == destination:
        raise FileExistsError('只允许新建暂存封面，不能覆盖现有资源。')
    if image_format not in ('JPEG', 'HEIC') or pixels.width < 1 or pixels.height < 1:
        raise ValueError('封面格式或尺寸无效。')
    source_state, source_hash = _signature(source), _sha(source, cancel_event)
    original_note, original_capture = _read_note(source, cancel_event), _capture(source, cancel_event)
    original_raw_capture = _capture_raw(source, cancel_event)
    created = False
    with tempfile.TemporaryDirectory(prefix='._lpimage-', dir=destination.parent) as directory:
        work = Path(directory)
        extension = '.jpg' if image_format == 'JPEG' else '.heic'
        bare, copied, final = (work / (name + extension) for name in ('pixels', 'copied', 'final'))
        try:
            rgb = pixels.convert('RGB')
            if image_format == 'JPEG':
                rgb.save(bare, 'JPEG', quality=100, subsampling=0)
            else:
                (heic_encoder or encode_heic)(rgb, bare, cancel_event=cancel_event)
            # Per-tag GPS copies pass through floating point/PrintConv and can
            # also add mandatory tags missing in the source. Inject only the
            # selected raw fields and complete MakerNote as a bounded block.
            metadata = work / 'capture.exif'
            metadata.write_bytes(_capture_block(original_raw_capture, original_note[0]))
            _run(['-EXIF<=' + str(metadata), '-o', copied, bare], cancel_event)
            _verify_capture_raw(original_raw_capture, _capture_raw(copied, cancel_event))
            now = datetime.now().astimezone()
            zone = now.strftime('%z')
            zone = zone[:3] + ':' + zone[3:]
            # Copy and ID editing require two invocations: a combined command
            # can silently leave the old ID despite exit code zero.
            _run(['-Apple:ContentIdentifier=' + identifier, '-IFD0:Orientation#=1',
                  f'-IFD0:ImageWidth={rgb.width}', f'-IFD0:ImageHeight={rgb.height}',
                  f'-ExifIFD:ExifImageWidth={rgb.width}', f'-ExifIFD:ExifImageHeight={rgb.height}',
                  '-IFD0:ModifyDate=' + now.strftime('%Y:%m:%d %H:%M:%S'),
                  '-ExifIFD:OffsetTime=' + zone, '-o', final, copied], cancel_event)
            verification = _verify_note(original_note, _read_note(final, cancel_event), identifier)
            _verify_capture_raw(original_raw_capture, _capture_raw(final, cancel_event))
            final_capture = _capture(final, cancel_event)
            if final_capture != original_capture:
                names = sorted(key for key in original_capture.keys() | final_capture.keys()
                               if original_capture.get(key) != final_capture.get(key))
                raise ValueError('拍摄元信息独立读回不一致：' + '、'.join(names))
            with Image.open(bare) as encoded, Image.open(final) as decoded:
                encoded.load(); decoded.load()
                if (decoded.format != ('JPEG' if image_format == 'JPEG' else 'HEIF')
                        or decoded.size != rgb.size or decoded.convert('RGB').tobytes() != encoded.convert('RGB').tobytes()):
                    raise ValueError('ExifTool 改变了新封面的像素、尺寸或格式。')
            import json
            properties = json.loads(_run(['-j', '-n', '-Orientation', '-ExifImageWidth', '-ExifImageHeight',
                                          '-Apple:ContentIdentifier', final], cancel_event))[0]
            if (properties.get('Orientation'), properties.get('ExifImageWidth'), properties.get('ExifImageHeight')) != (1, rgb.width, rgb.height):
                raise ValueError('新封面的方向或尺寸元数据不正确。')
            if properties.get('ContentIdentifier') != identifier:
                raise ValueError('ExifTool 独立读回的新配对标识不正确。')
            _same(source, source_state, source_hash, cancel_event)
            result_hash = _sha(final, cancel_event)
            _copy_exclusive(final, destination, cancel_event)
            created = True
            _same(source, source_state, source_hash, cancel_event)
            _cancel(cancel_event)
            return {'schema': 'liveviewer.native-image.v1', 'asset_identifier': identifier,
                    'image_format': image_format, 'size': list(rgb.size), 'source_sha256': source_hash,
                    'output_sha256': result_hash, 'capture_tags_preserved': True,
                    'capture_typed_raw_fields_preserved': True,
                    'capture_field_names': [_capture_name(group, tag)
                        for group in ('IFD0', 'ExifIFD', 'GPS') for tag in sorted(original_raw_capture[group])],
                    'pixels_after_metadata_write_identical': True, **verification}
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise


def _sample_digests(loaded, event):
    result = []
    with loaded.path.open('rb') as stream:
        for track in loaded.tracks:
            samples = []
            for offset, size in track.samples:
                _cancel(event)
                stream.seek(offset)
                data = stream.read(size)
                if len(data) != size:
                    raise ValueError('MOV 样本读取截断。')
                samples.append((size, hashlib.sha256(data).hexdigest()))
            result.append((track.identifier, track.handler, track.scale, track.duration,
                           track.times, track.visible_times, samples))
    return result


def _track_structure_without_offsets(track):
    mdia = movie._one(movie._children(track.box), b'mdia')
    minf = movie._one(movie._children(mdia), b'minf')
    stbl = movie._atom(b'stbl', b''.join(box.data for box in movie._children(track.stbl)
                                       if box.kind not in (b'stco', b'co64')))
    return movie._replace(track.box, {b'mdia': movie._replace(mdia, {b'minf': movie._replace(minf, {b'stbl': stbl})})})


def verify_native_movie_payload_preserved(source, destination, cancel_event=None):
    """Verify original A/V bytes and timing after a legal chunk relocation.

    Only stco/co64 absolute offsets may differ. Pair identifiers and the still
    marker intentionally have separate validation in the saving transaction.
    """
    before, after = movie._load(source, cancel_event), movie._load(destination, cancel_event)
    old = {track.identifier: track for track in before.tracks if track.handler in (b'vide', b'soun')}
    new = {track.identifier: track for track in after.tracks if track.handler in (b'vide', b'soun')}
    if (before.scale, before.duration) != (after.scale, after.duration) or old.keys() != new.keys():
        raise ValueError('原音视频轨道集合或影片时钟发生改变。')
    for identifier, track in old.items():
        _cancel(cancel_event)
        other = new[identifier]
        if (track.handler != other.handler
                or _track_structure_without_offsets(track) != _track_structure_without_offsets(other)):
            raise ValueError('原音视频轨道除块地址外的结构发生改变。')
    old_samples = {row[0]: row for row in _sample_digests(before, cancel_event) if row[1] in (b'vide', b'soun')}
    new_samples = {row[0]: row for row in _sample_digests(after, cancel_event) if row[1] in (b'vide', b'soun')}
    if old_samples != new_samples:
        raise ValueError('原音视频编码样本或精确呈现时间发生改变。')
    if before.signature != movie._signature(before.path) or after.signature != movie._signature(after.path):
        raise OSError('音视频校验期间文件发生改变。')
    _cancel(cancel_event)
    return {'audio_video_track_ids': sorted(old), 'all_encoded_samples_and_timing_preserved': True,
            'all_track_structure_except_chunk_offsets_preserved': True}


def _movie_edit(source, output, identifier, event):
    from .live_photo_native_movie import _global_non_identifier_fields
    before = movie._load(source, event)
    state, source_hash = _signature(source), _sha(source, event)
    old_info = movie._info(before)
    sample_state = _sample_digests(before, event)
    _run(['-Keys:ContentIdentifier=' + identifier, '-o', output, source], event)
    after = movie._load(output, event)
    new_info = movie._info(after)
    if new_info.asset_identifier != identifier:
        raise ValueError('MOV 配对标识读回不一致。')
    if (before.scale, before.duration, old_info.photo_time, old_info.marker_count) != (
            after.scale, after.duration, new_info.photo_time, new_info.marker_count):
        raise ValueError('ExifTool 改变了 MOV 时间或封面标记。')
    if sample_state != _sample_digests(after, event):
        raise ValueError('ExifTool 改变或丢失了原 MOV 轨道样本及时间。')
    if [_track_structure_without_offsets(track) for track in before.tracks] != [
            _track_structure_without_offsets(track) for track in after.tracks]:
        raise ValueError('ExifTool 改变了原 MOV 轨道属性。')
    if _global_non_identifier_fields(before) != _global_non_identifier_fields(after):
        raise ValueError('ExifTool 改变了非配对的全局 MOV 元数据。')
    _same(source, state, source_hash, event)
    return {'schema': 'liveviewer.native-movie-id.v1', 'asset_identifier': identifier,
            'source_sha256': source_hash, 'output_sha256': _sha(output, event),
            'marker_time': str(new_info.photo_time) if new_info.photo_time is not None else None,
            'marker_count': new_info.marker_count, 'track_count': len(after.tracks),
            'all_track_samples_and_times_preserved': True, 'track_properties_except_chunk_offsets_preserved': True,
            'non_identifier_global_metadata_preserved': True}


def copy_movie_with_identifier(source, destination, identifier, cancel_event=None):
    identifier = _identifier(identifier)
    source, destination = _path(source), _path(destination, existing=False)
    if destination.exists() or source == destination:
        raise FileExistsError('只允许新建 MOV 副本。')
    state, source_hash = _signature(source), _sha(source, cancel_event)
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix='._lpmovie-', dir=destination.parent) as directory:
            final = Path(directory) / 'final.mov'
            report = _movie_edit(source, final, identifier, cancel_event)
            _same(source, state, source_hash, cancel_event)
            _copy_exclusive(final, destination, cancel_event)
            created = True
            _same(source, state, source_hash, cancel_event)
            _cancel(cancel_event)
            return report
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def set_movie_identifier(new_existing_staged_movie, identifier, cancel_event=None):
    """Only call on the caller's newly generated private stage, never an input."""
    identifier = _identifier(identifier)
    staged = _path(new_existing_staged_movie)
    state, source_hash = _signature(staged), _sha(staged, cancel_event)
    with tempfile.TemporaryDirectory(prefix='._lpmovie-', dir=staged.parent) as directory:
        final = Path(directory) / 'final.mov'
        report = _movie_edit(staged, final, identifier, cancel_event)
        _same(staged, state, source_hash, cancel_event)
        _cancel(cancel_event)
        os.replace(final, staged)
        return report
