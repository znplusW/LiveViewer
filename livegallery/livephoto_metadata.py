"""Live Photo pairing and a real timed still-image sample, without A/V re-encoding.

QuickTime: keep every existing media offset and every audio/video track intact;
retire the old moov as free space and append a replacement index plus one metadata
sample. Fragmented/external-reference movies and mixed still metadata tracks are
rejected rather than partially rewritten. Apple MakerNote 17 holds the same UUID.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import struct
import time
import uuid

from PIL import Image

CONTENT_ID = b'com.apple.quicktime.content.identifier'
STILL_TIME = b'com.apple.quicktime.still-image-time'
MAX_MOOV = 64 * 1024 * 1024
SCALE = 1_000_000
IDENTITY = struct.pack('>9I', 65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)


class LivePhotoMetadataError(ValueError):
    pass


def _fail(message='视频元信息结构不受支持，未更改原文件。'):
    raise LivePhotoMetadataError(message)


def _u32(data, offset=0):
    if offset < 0 or offset + 4 > len(data):
        _fail()
    return struct.unpack_from('>I', data, offset)[0]


def _box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def _full(kind, payload, flags=0):
    return _box(kind, struct.pack('>I', flags) + payload)


@dataclass(frozen=True)
class Box:
    kind: bytes
    offset: int
    size: int
    header: int


def _boxes(data):
    position = 0
    while position < len(data):
        if len(data) - position < 8:
            _fail()
        size, kind = struct.unpack_from('>I4s', data, position)
        header = 8
        if size == 1:
            if len(data) - position < 16:
                _fail()
            size = struct.unpack_from('>Q', data, position + 8)[0]
            header = 16
        if size == 0:
            size = len(data) - position
        if size < header or position + size > len(data):
            _fail()
        yield Box(kind, position, size, header)
        position += size


def _children(data):
    return [(box.kind, data[box.offset + box.header:box.offset + box.size]) for box in _boxes(data)]


def _one(children, kind):
    matches = [value for key, value in children if key == kind]
    if len(matches) != 1:
        _fail()
    return matches[0]


def _top(path):
    size = Path(path).stat().st_size
    position = 0
    result = []
    with Path(path).open('rb') as stream:
        while position < size:
            stream.seek(position)
            prefix = stream.read(16)
            if len(prefix) < 8:
                _fail()
            length, kind = struct.unpack_from('>I4s', prefix)
            header = 8
            if length == 1:
                if len(prefix) < 16:
                    _fail()
                length = struct.unpack_from('>Q', prefix, 8)[0]
                header = 16
            if length == 0:
                # Appending after an unbounded mdat would hide the new index.
                _fail('视频含未封闭的数据段，暂不支持更改此 Live Photo 的封面。')
            if length < header or position + length > size:
                _fail()
            result.append(Box(kind, position, length, header))
            position += length
    return result


def _movie(path):
    boxes = _top(path)
    if any(box.kind in (b'moof', b'mfra') for box in boxes):
        _fail('暂不支持分段视频的 Live Photo 元信息写入。')
    moovs = [box for box in boxes if box.kind == b'moov']
    if len(moovs) != 1 or moovs[0].size > MAX_MOOV:
        _fail()
    moov = moovs[0]
    with Path(path).open('rb') as stream:
        stream.seek(moov.offset + moov.header)
        data = stream.read(moov.size - moov.header)
    return moov, _children(data)


def _meta_parts(data):
    # QuickTime meta has no full-box header; ISO meta has four version bytes.
    prefix = b'\0' * 4 if data[:4] == b'\0' * 4 else b''
    children = _children(data[len(prefix):])
    keys = _one(children, b'keys')
    if len(keys) < 8 or keys[:4] != b'\0' * 4:
        _fail()
    entries = _children(keys[8:])
    if _u32(keys, 4) != len(entries):
        _fail()
    items = _children(_one(children, b'ilst'))
    return prefix, children, entries, items


def _content_meta(data, identifier):
    if data is None:
        children = [(b'hdlr', b'\0' * 8 + b'mdta' + b'\0' * 13)]
        prefix, entries, items = b'', [], []
    else:
        prefix, children, entries, items = _meta_parts(data)
    indices = [i + 1 for i, pair in enumerate(entries) if pair == (b'mdta', CONTENT_ID)]
    if len(indices) > 1:
        _fail()
    if indices:
        index = indices[0]
    else:
        entries.append((b'mdta', CONTENT_ID))
        index = len(entries)
    key = struct.pack('>I', index)
    items = [(kind, payload) for kind, payload in items if kind != key]
    items.append((key, _box(b'data', struct.pack('>II', 1, 0) + identifier.encode('ascii'))))
    keys = _full(b'keys', struct.pack('>I', len(entries)) + b''.join(_box(*pair) for pair in entries))
    ilst = _box(b'ilst', b''.join(_box(*pair) for pair in items))
    other = b''.join(_box(*pair) for pair in children if pair[0] not in (b'keys', b'ilst'))
    return _box(b'meta', prefix + other + keys + ilst)


def _track_info(payload):
    track = _children(payload)
    tkhd = _one(track, b'tkhd')
    identifier = _u32(tkhd, 20 if tkhd[0] == 1 else 12)
    mdia = _children(_one(track, b'mdia'))
    handler = _one(mdia, b'hdlr')[8:12]
    minf = _children(_one(mdia, b'minf'))
    # No external data references: preserved offsets must refer to this MOV.
    dref = _one(_children(_one(minf, b'dinf')), b'dref')
    refs = _children(dref[8:])
    if _u32(dref, 4) != len(refs) or any(len(value) < 4 or not _u32(value) & 1 for _, value in refs):
        _fail('视频引用了外部媒体，暂不支持更改封面。')
    stbl = _children(_one(minf, b'stbl'))
    stsd = _one(stbl, b'stsd')
    return identifier, handler, stsd, track, mdia, stbl


def _marker_track(track_id, movie_scale, start_seconds, duration_seconds, offset, created):
    start = round(start_seconds * movie_scale)
    interval = max(1, round(duration_seconds * movie_scale))
    media_interval = max(1, round(duration_seconds * SCALE))
    tkhd = _full(b'tkhd', struct.pack('>5I', created, created, track_id, 0, start + interval)
                 + b'\0' * 16 + IDENTITY + b'\0' * 8, flags=7)
    edits = []
    if start:
        edits.append(struct.pack('>IiHH', start, -1, 1, 0))
    edits.append(struct.pack('>IiHH', interval, 0, 1, 0))
    edts = _box(b'edts', _full(b'elst', struct.pack('>I', len(edits)) + b''.join(edits)))
    mdhd = _full(b'mdhd', struct.pack('>4IHH', created, created, SCALE, media_interval, 0x55c4, 0))
    hdlr = _full(b'hdlr', b'mhlrmetaappl' + b'\0' * 8 + b'\x13Core Media Metadata')
    # QuickTime mebx key declaration: datatype 65 is signed 8-bit integer.
    key = _box(b'keyd', b'mdta' + STILL_TIME) + _box(b'dtyp', struct.pack('>II', 0, 65))
    declaration = _box(b'keys', _box(struct.pack('>I', 1), key))
    stsd = _full(b'stsd', struct.pack('>I', 1) + _box(b'mebx', b'\0' * 6 + b'\0\1' + declaration))
    stts = _full(b'stts', struct.pack('>III', 1, 1, media_interval))
    stsc = _full(b'stsc', struct.pack('>4I', 1, 1, 1, 1))
    stsz = _full(b'stsz', struct.pack('>II', 9, 1))
    co64 = _full(b'co64', struct.pack('>IQ', 1, offset))
    gmhd = _box(b'gmhd', _full(b'gmin', struct.pack('>6H', 64, 32768, 32768, 32768, 0, 0)))
    dinf = _box(b'dinf', _full(b'dref', struct.pack('>I', 1) + _full(b'url ', b'', flags=1)))
    minf = _box(b'minf', gmhd + dinf + _box(b'stbl', stsd + stts + stsc + stsz + co64))
    return _box(b'trak', tkhd + edts + _box(b'mdia', mdhd + hdlr + minf))


def rewrite_movie(path: Path, identifier: str, seconds: float, duration: float, *, cancel_event=None):
    """Modify a private staged copy only; original media bytes never move."""
    uuid.UUID(identifier)
    if not all(math.isfinite(x) for x in (seconds, duration)) or seconds < 0 or duration <= 0:
        _fail('所选帧时间无效，无法保存 Live Photo。')
    old, children = _movie(path)
    mvhd = bytearray(_one(children, b'mvhd'))
    version = mvhd[0]
    if version not in (0, 1):
        _fail()
    scale = _u32(mvhd, 20 if version else 12)
    movie_duration = struct.unpack_from('>Q' if version else '>I', mvhd, 24 if version else 16)[0]
    if not scale or seconds >= movie_duration / scale + .001:
        _fail('所选封面时刻超出了视频范围。')
    duration = min(duration, movie_duration / scale - seconds)
    if duration <= 0:
        _fail()
    kept, ids, meta = [], [], None
    av_count = 0
    for kind, payload in children:
        if kind == b'trak':
            tid, handler, stsd, *_ = _track_info(payload)
            ids.append(tid)
            if STILL_TIME in stsd:
                # A dedicated single-key marker track is safe to replace. Do not
                # discard other sensor/timed metadata from a multiplexed track.
                if handler != b'meta' or stsd.count(b'keyd') != 1 or b'mebx' not in stsd:
                    _fail('此视频的封面标记与其他元信息混合存储，暂不支持安全修改。')
                continue
            av_count += handler == b'vide'
        if kind == b'udta':
            user_data = []
            for subkind, subvalue in _children(payload):
                if subkind == b'meta' and b'mdta' in subvalue:
                    if meta is not None:
                        _fail()
                    meta = subvalue
                else:
                    user_data.append(_box(subkind, subvalue))
            kept.append(_box(b'udta', b''.join(user_data)))
            continue
        if kind == b'meta' and b'mdta' in payload:
            if meta is not None:
                _fail()
            meta = payload
            continue
        if kind not in (b'mvhd',):
            kept.append(_box(kind, payload))
    if not av_count or not ids:
        _fail()
    new_id = max(ids) + 1
    now = int(time.time()) + 2082844800
    struct.pack_into('>Q' if version else '>I', mvhd, 12 if version else 8, now)
    struct.pack_into('>I', mvhd, len(mvhd) - 4, new_id + 1)
    sample = _box(struct.pack('>I', 1), b'\0')
    offset = Path(path).stat().st_size + 8
    track = _marker_track(new_id, scale, seconds, duration, offset, now)
    updated = _box(b'moov', _box(b'mvhd', bytes(mvhd)) + b''.join(kept) + track + _content_meta(meta, identifier))
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('已取消保存。')
    import os
    with Path(path).open('r+b') as stream:
        stream.seek(0, 2)
        stream.write(_box(b'mdat', sample))
        stream.write(updated)
        stream.seek(old.offset + 4)
        stream.write(b'free')
        stream.flush()
        os.fsync(stream.fileno())


def movie_pairing(path):
    """Read content ID and actual marker sample timeline for save validation."""
    _, children = _movie(path)
    mvhd = _one(children, b'mvhd')
    scale = _u32(mvhd, 20 if mvhd[0] else 12)
    identifiers, markers = [], []
    for kind, payload in children:
        if kind == b'meta' and CONTENT_ID in payload:
            _, _, keys, items = _meta_parts(payload)
            for index, (namespace, key) in enumerate(keys, 1):
                if namespace == b'mdta' and key == CONTENT_ID:
                    item = _one(items, struct.pack('>I', index))
                    data = _one(_children(item), b'data')
                    if _u32(data) != 1:
                        _fail()
                    identifiers.append(data[8:].decode('ascii'))
        if kind == b'trak':
            _, _, stsd, track, mdia, stbl = _track_info(payload)
            if STILL_TIME not in stsd:
                continue
            elst = _one(_children(_one(track, b'edts')), b'elst')
            if elst[0] != 0:
                _fail()
            start = 0
            for index in range(_u32(elst, 4)):
                length, media_time, rate, fraction = struct.unpack_from('>IiHH', elst, 8 + 12 * index)
                if (rate, fraction) != (1, 0):
                    _fail()
                if media_time == -1:
                    start += length
                else:
                    if media_time != 0:
                        _fail()
                    break
            sizes = _one(stbl, b'stsz')
            if _u32(sizes, 4) != 9 or _u32(sizes, 8) != 1:
                _fail()
            offsets = [(key, value) for key, value in stbl if key in (b'stco', b'co64')]
            if len(offsets) != 1 or _u32(offsets[0][1], 4) != 1:
                _fail()
            key, value = offsets[0]
            offset = struct.unpack_from('>Q' if key == b'co64' else '>I', value, 8)[0]
            with Path(path).open('rb') as stream:
                stream.seek(offset)
                if stream.read(9) != b'\0\0\0\x09\0\0\0\x01\0':
                    _fail()
            markers.append(start / scale)
    if len(identifiers) != 1 or len(markers) != 1:
        _fail('Live Photo 配对标识或封面时间标记不完整。')
    return identifiers[0], markers[0], 1 / scale


def make_photo_exif(source: Path, identifier: str, size: tuple[int, int]) -> bytes:
    """Keep capture date/location, drop stale sensor/pixel metadata of old cover."""
    uuid.UUID(identifier)
    from . import imaging  # Register HEIC decoder.
    exif = Image.Exif()
    original = {}
    with Image.open(source) as photo:
        old = photo.getexif()
        for key in (271, 272, 315):  # camera make/model, artist
            if key in old:
                exif[key] = old[key]
        original = old.get_ifd(34665)
        gps = old.get_ifd(34853)
        if gps:
            exif[34853] = gps
    value = identifier.encode('ascii') + b'\0'
    # Apple iOS header (14 bytes), big-endian IFD. Offsets relative to header.
    maker = b'Apple iOS\0\0\1MM' + struct.pack('>H', 1)
    maker += struct.pack('>HHII', 17, 2, len(value), 32) + b'\0' * 4 + value
    detail = {key: original[key] for key in (36867, 36868, 36880, 36881, 36882, 37521, 37522) if key in original}
    detail.update({37500: maker, 40962: size[0], 40963: size[1], 40961: 1})
    exif[34665] = detail
    exif[274] = 1
    exif[305] = 'LiveViewer'
    exif[306] = datetime.now().strftime('%Y:%m:%d %H:%M:%S')
    encoded = bytearray(exif.tobytes())
    # Pillow serializes nested byte values as TIFF BYTE (1). ImageIO requires
    # MakerNote's specified UNDEFINED (7) type to expose MakerApple[17].
    base = 6  # Exif\0\0 prefix
    ifd = struct.unpack_from('>I', encoded, base + 4)[0]
    for index in range(struct.unpack_from('>H', encoded, base + ifd)[0]):
        entry = base + ifd + 2 + 12 * index
        if struct.unpack_from('>H', encoded, entry)[0] == 34665:
            sub = base + struct.unpack_from('>I', encoded, entry + 8)[0]
            for j in range(struct.unpack_from('>H', encoded, sub)[0]):
                nested = sub + 2 + 12 * j
                if struct.unpack_from('>H', encoded, nested)[0] == 37500:
                    struct.pack_into('>H', encoded, nested + 2, 7)
    return bytes(encoded)


def photo_identifier(path):
    from . import imaging
    with Image.open(path) as photo:
        raw = photo.info.get('exif', b'')
        raw = raw[6:] if raw.startswith(b'Exif\0\0') else raw
        if len(raw) < 8 or raw[:2] not in (b'MM', b'II'):
            _fail('新图片缺少有效 EXIF 元信息。')
        order = '>' if raw[:2] == b'MM' else '<'
        def entry_offset(ifd, wanted):
            if ifd < 0 or ifd + 2 > len(raw):
                _fail()
            count = struct.unpack_from(order + 'H', raw, ifd)[0]
            if ifd + 2 + count * 12 > len(raw):
                _fail()
            for i in range(count):
                offset = ifd + 2 + i * 12
                if struct.unpack_from(order + 'H', raw, offset)[0] == wanted:
                    return offset
            _fail('新图片的 EXIF 关联信息缺失。')
        parent = entry_offset(struct.unpack_from(order + 'I', raw, 4)[0], 34665)
        entry = entry_offset(struct.unpack_from(order + 'I', raw, parent + 8)[0], 37500)
        if struct.unpack_from(order + 'H', raw, entry + 2)[0] != 7:
            _fail('新图片的 Apple 关联信息存储类型不正确。')
        note = photo.getexif().get_ifd(34665).get(37500, b'')
    if not note.startswith(b'Apple iOS\0\0\1MM') or len(note) < 16:
        _fail('新图片缺少 Apple Live Photo 关联标识。')
    count = struct.unpack_from('>H', note, 14)[0]
    values = []
    for i in range(count):
        if 16 + 12 * (i + 1) > len(note):
            _fail()
        tag, kind, length, offset = struct.unpack_from('>HHII', note, 16 + 12 * i)
        if tag == 17:
            if kind != 2 or length < 2 or offset + length > len(note):
                _fail()
            values.append(note[offset:offset + length].rstrip(b'\0').decode('ascii'))
    if len(values) != 1:
        _fail('新图片的 Live Photo 标识不完整。')
    return values[0]


def validate_pair(image, video, identifier, seconds):
    movie_id, marker, tolerance = movie_pairing(video)
    if photo_identifier(image) != identifier or movie_id != identifier:
        _fail('图片与视频的 Live Photo 关联标识不一致，已取消保存。')
    if abs(marker - seconds) > tolerance + 1e-6:
        _fail('封面图与视频中的标记时刻不一致，已取消保存。')
    return {'identifier': identifier, 'still_time_seconds': marker}
