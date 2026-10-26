"""Conservative, lossless QuickTime Live Photo metadata rewriting.

No encoder is involved. Existing audio/video chunks keep their offsets and
bytes. Only the active movie box and timed metadata sample tables are changed.
photo_time is an exact *movie presentation* time, after the video edit list.

Format references (implementation is original, no third-party code copied):
https://developer.apple.com/documentation/quicktime-file-format/overview_of_timed_metadata
https://developer.apple.com/documentation/quicktime-file-format/timed_metadata_sample_descriptions
https://developer.apple.com/documentation/quicktime-file-format/constant-size_timed_metadata_sample_data
https://developer.apple.com/documentation/quicktime-file-format/metadata_atoms_and_types

Fragmented/encrypted/external media, multiple video tracks and complex edits
are rejected. This file does not claim Apple Photos import certification.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import math
import os
from pathlib import Path
import struct


CONTENT_KEY = b"com.apple.quicktime.content.identifier"
STILL_KEY = b"com.apple.quicktime.still-image-time"
MAX_MOOV = 64 * 1024 * 1024
MAX_SAMPLES = 200_000
MAX_METADATA = 32 * 1024 * 1024
MAX_BOXES = 200_000
U32 = 0xffffffff


class LivePhotoMovieError(ValueError):
    """Unsupported or ambiguous media; no source file has been changed."""


@dataclass(frozen=True)
class LivePhotoMovieInfo:
    asset_identifier: str | None = None
    photo_time: Fraction | None = None
    duration: Fraction | None = None
    supported: bool = False
    unsupported_reason: str | None = None
    marker_count: int = 0
    track_count: int = 0
    media_payload_digests: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class _Box:
    kind: bytes
    start: int
    size: int
    header: int
    data: bytes = b""

    @property
    def payload(self):
        return self.data[self.header:]


@dataclass
class _Track:
    box: _Box
    identifier: int
    handler: bytes
    scale: int
    duration: int
    stbl: _Box
    sample_entries: list[_Box]
    samples: list[tuple[int, int]]
    times: list[Fraction]
    visible_times: list[Fraction]
    marker_ids: set[bytes]
    keys: list[_Box]
    marker_times: list[Fraction]
    metadata_samples: list[bytes]


@dataclass
class _Movie:
    path: Path
    signature: tuple
    top: list[_Box]
    moov: _Box
    children: list[_Box]
    scale: int
    duration: int
    tracks: list[_Track]
    asset_identifier: str | None


def _check(value, message):
    if not value:
        raise LivePhotoMovieError(message)


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("已取消 Live Photo 视频保存。")


def _u32(data, offset=0):
    _check(offset + 4 <= len(data), "MOV 数据字段截断。")
    return struct.unpack_from(">I", data, offset)[0]


def _pack(value):
    _check(0 <= value <= U32, "MOV 整数字段超过支持范围。")
    return struct.pack(">I", value)


def _atom(kind, payload=b""):
    _check(len(kind) == 4 and len(payload) + 8 <= U32, "MOV atom 大小超限。")
    return _pack(len(payload) + 8) + kind + payload


def _full(kind, payload=b"", version=0, flags=0):
    return _atom(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _boxes(data, start=0):
    result = []
    pos = start
    while pos < len(data):
        _check(len(result) < MAX_BOXES and pos + 8 <= len(data), "MOV atom 边界无效。")
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            _check(pos + 16 <= len(data), "MOV 64位 atom 截断。")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        _check(size >= header and pos + size <= len(data), "MOV atom 长度无效或未限定。")
        result.append(_Box(kind, pos, size, header, data[pos:pos + size]))
        pos += size
    return result


def _one(boxes, kind, optional=False):
    matches = [b for b in boxes if b.kind == kind]
    _check(len(matches) == 1 or (optional and not matches), f"MOV {kind!r} 缺失或重复。")
    return matches[0] if matches else None


def _children(box):
    return _boxes(box.payload)


def _replace(box, replacements):
    return _atom(box.kind, b"".join(replacements.get(b.kind, b.data) for b in _children(box)))


def _signature(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino


def _top(stream, length):
    result = []
    pos = 0
    while pos < length:
        _check(len(result) < MAX_BOXES and pos + 8 <= length, "MOV 顶层 atom 截断。")
        stream.seek(pos)
        size, kind = struct.unpack(">I4s", stream.read(8))
        header = 8
        if size == 1:
            _check(pos + 16 <= length, "MOV 64位 atom 截断。")
            size = struct.unpack(">Q", stream.read(8))[0]
            header = 16
        elif size == 0:
            size = length - pos
            _check(size <= U32 and kind == b"mdat", "不支持未限定的巨大 MOV atom。")
        _check(size >= header and pos + size <= length, "MOV 顶层 atom 长度无效。")
        result.append(_Box(kind, pos, size, header))
        pos += size
    return result


def _clock(box):
    p = box.payload
    _check(len(p) >= 24 and p[0] in (0, 1) and p[1:4] == b"\0" * 3, "MOV 时钟版本不支持。")
    offset = 12 if p[0] == 0 else 20
    scale = _u32(p, offset)
    if p[0] == 0:
        duration = _u32(p, offset + 4)
    else:
        _check(len(p) >= offset + 12, "MOV 64位时钟截断。")
        duration = struct.unpack_from(">Q", p, offset + 4)[0]
    _check(scale > 0 and duration > 0, "MOV 时间基或时长无效。")
    return scale, duration


def _table(box, columns, signed_last=False):
    p = box.payload
    _check(len(p) >= 8 and p[0] in ((0, 1) if signed_last else (0,))
           and p[1:4] == b"\0" * 3, "MOV 表头不支持。")
    count = _u32(p, 4)
    _check(count <= MAX_SAMPLES and len(p) == 8 + count * columns * 4, "MOV 表长度超限或无效。")
    fmt = ">" + "I" * (columns - 1) + ("i" if signed_last and p[0] == 1 else "I")
    return [struct.unpack_from(fmt, p, 8 + i * columns * 4) for i in range(count)]


def _sample_ranges(stbl, entries, mdats):
    boxes = _children(stbl)
    _check(not any(b.kind in {b"saio", b"senc", b"saiz", b"stz2"} for b in boxes), "不支持加密或紧凑采样表。")
    sz = _one(boxes, b"stsz").payload
    _check(len(sz) >= 12 and sz[:4] == b"\0" * 4, "MOV 样本大小表不支持。")
    fixed, count = _u32(sz, 4), _u32(sz, 8)
    _check(0 < count <= MAX_SAMPLES and len(sz) == 12 + (0 if fixed else 4 * count), "MOV 样本数超限或无效。")
    sizes = [fixed] * count if fixed else [_u32(sz, 12 + 4 * i) for i in range(count)]
    co = [b for b in boxes if b.kind in {b"stco", b"co64"}]
    _check(len(co) == 1, "MOV chunk 偏移表缺失或重复。")
    p = co[0].payload
    width = 8 if co[0].kind == b"co64" else 4
    chunks = _u32(p, 4)
    _check(p[:4] == b"\0" * 4 and 0 < chunks <= MAX_SAMPLES and len(p) == 8 + width * chunks, "MOV chunk 偏移表无效。")
    offsets = [int.from_bytes(p[8 + width * i:8 + width * (i + 1)], "big") for i in range(chunks)]
    sc = _table(_one(boxes, b"stsc"), 3)
    _check(sc and sc[0][0] == 1 and all(a[0] < b[0] for a, b in zip(sc, sc[1:])), "MOV chunk 映射无效。")
    _check(all(0 < first <= chunks and 0 < n <= count and 1 <= desc <= len(entries) for first, n, desc in sc), "MOV chunk 描述索引无效。")
    result = []
    run = 0
    for i, offset in enumerate(offsets, 1):
        while run + 1 < len(sc) and sc[run + 1][0] <= i:
            run += 1
        n = sc[run][1]
        _check(len(result) + n <= count, "MOV chunk 样本数溢出。")
        for size in sizes[len(result):len(result) + n]:
            _check(any(lo <= offset and offset + size <= hi for lo, hi in mdats), "MOV 样本不在本文件 mdat 内。")
            result.append((offset, size))
            offset += size
    _check(len(result) == count, "MOV 样本计数不一致。")
    return result


def _presentation_times(track, stbl, scale, count, movie_scale):
    rows = _table(_one(_children(stbl), b"stts"), 2)
    _check(sum(n for n, _ in rows) == count and all(n and d for n, d in rows), "MOV DTS 表无效。")
    ticks = []
    elapsed = 0
    for n, delta in rows:
        for _ in range(n):
            ticks.append(elapsed)
            elapsed += delta
    ctts = _one(_children(stbl), b"ctts", optional=True)
    if ctts:
        composition = _table(ctts, 2, signed_last=True)
        _check(sum(n for n, _ in composition) == count, "MOV PTS 表计数不一致。")
        offsets = [offset for n, offset in composition for _ in range(n)]
        ticks = [tick + offset for tick, offset in zip(ticks, offsets)]
    times = [Fraction(tick, scale) for tick in ticks]
    tkhd = _one(_children(track), b"tkhd").payload
    duration_offset, duration_width = (28, 8) if tkhd[0] == 1 else (20, 4)
    _check(len(tkhd) >= duration_offset + duration_width, "MOV track 时长截断。")
    track_duration = int.from_bytes(tkhd[duration_offset:duration_offset + duration_width], "big")
    visible_start, visible_end = Fraction(0), Fraction(track_duration, movie_scale)
    edts = _one(_children(track), b"edts", optional=True)
    if edts:
        elst = _one(_children(edts), b"elst").payload
        _check(len(elst) >= 8 and elst[0] in (0, 1) and elst[1:4] == b"\0" * 3, "MOV edit list 版本不支持。")
        version, n = elst[0], _u32(elst, 4)
        width = 20 if version else 12
        _check(n in (1, 2) and len(elst) == 8 + width * n, "不支持多段 MOV edit list。")
        edits = [struct.unpack_from(">Qqhh" if version else ">Iihh", elst, 8 + i * width) for i in range(n)]
        _check(all(rate == 1 and frac == 0 for _, _, rate, frac in edits), "不支持变速 MOV edit list。")
        delay = Fraction(0)
        if n == 2:
            _check(edits[0][1] == -1, "仅支持首段为空白的 MOV edit list。")
            delay = Fraction(edits[0][0], movie_scale)
        duration, media_time, _, _ = edits[-1]
        _check(media_time >= 0 and duration > 0, "MOV 有效 edit 区间无效。")
        times = [t - Fraction(media_time, scale) + delay for t in times]
        visible_start, visible_end = delay, delay + Fraction(duration, movie_scale)
    return times, [t for t in times if visible_start <= t < visible_end]


def _metadata_extensions(entry):
    _check(entry.kind == b"mebx" and len(entry.payload) >= 16, "不支持此 timed metadata 样本格式。")
    _check(entry.payload[:6] == b"\0" * 6 and entry.payload[6:8] == b"\0\1",
           "不支持此 mebx 样本头。")
    # QTFF's standard sample description is 16 bytes INCLUDING size/type:
    # reserved[6] + data_reference_index[2], then the keys extension directly.
    # The initial Windows test build emitted exactly eight extra zero bytes.
    # Read only that precise legacy shape, never scan for a plausible key box;
    # all rewritten metadata descriptions below use the canonical 8-byte prefix.
    start = 8
    if entry.payload[8:16] == b"\0" * 8:
        _check(len(entry.payload) >= 24 and entry.payload[20:24] == b"keys",
               "旧版 mebx 填充后缺少有效 keys 容器。")
        start = 16
    extensions = _boxes(entry.payload, start)
    _one(extensions, b"keys")
    return extensions


def _metadata_layout(entry):
    extensions = _metadata_extensions(entry)
    keys = _children(_one(extensions, b"keys"))
    _check(len({key.kind for key in keys}) == len(keys), "mebx key ID 重复。")
    marker_ids = set()
    for key in keys:
        if key.kind == b"\0" * 4:
            continue
        _check(key.kind != b"\xff" * 4, "mebx 使用保留 key ID。")
        definition = _one(_children(key), b"keyd").payload
        if definition == b"mdta" + STILL_KEY:
            dtype = _one(_children(key), b"dtyp").payload
            _check(dtype == struct.pack(">II", 0, 65), "still-image-time 类型不是 int8。")
            marker_ids.add(key.kind)
    _check(len(marker_ids) <= 1, "同一轨道出现多个 still-image-time key。")
    return keys, marker_ids


def _meta_layout(box):
    # QuickTime has no FullBox header; ISO meta has four version/flag bytes.
    p = box.payload
    prefix = b"\0" * 4 if p[:4] == b"\0" * 4 else b""
    children = _boxes(p, len(prefix))
    hdlr = _one(children, b"hdlr", optional=True)
    if not hdlr or len(hdlr.payload) < 12 or hdlr.payload[8:12] != b"mdta":
        _check(CONTENT_KEY not in p and STILL_KEY not in p, "未知 metadata 容器包含 Live Photo key。")
        return None
    keys_box = _one(children, b"keys")
    keys_payload = keys_box.payload
    _check(keys_payload[:4] == b"\0" * 4 and len(keys_payload) >= 8, "mdta keys 表不支持。")
    keys = _boxes(keys_payload, 8)
    _check(len(keys) == _u32(keys_payload, 4), "mdta keys 计数不一致。")
    _check(not any(k.kind == b"mdta" and k.payload == STILL_KEY for k in keys), "发现非时序 still-image-time 标签，拒绝伪标记。")
    ilst = _one(children, b"ilst")
    values = _children(ilst)
    _check(all(1 <= int.from_bytes(b.kind, 'big') <= len(keys) for b in values), "mdta ilst 索引无效。")
    ids = {i + 1 for i, k in enumerate(keys) if k.kind == b"mdta" and k.payload == CONTENT_KEY}
    identifiers = []
    for item in values:
        if int.from_bytes(item.kind, "big") in ids:
            data = _one(_children(item), b"data").payload
            _check(len(data) > 8 and _u32(data) == 1, "ContentIdentifier 编码不是 UTF-8。")
            try:
                value = data[8:].decode("utf-8")
            except UnicodeError as error:
                raise LivePhotoMovieError("ContentIdentifier UTF-8 无效。") from error
            _check(value and "\0" not in value, "ContentIdentifier 为空或含NULL。")
            identifiers.append(value)
    return prefix, children, keys, values, ids, identifiers


def _reject_unhandled_track_pairing(box, depth=0):
    # QTFF also permits metadata at track/media scope. Changing those boxes
    # would violate the untouched audio/video-track contract, and leaving an
    # old pairing ID there would produce an ambiguous asset. Inspect only
    # real container children, never codec bytes or mdat string coincidences.
    _check(depth <= 8, "MOV metadata 容器嵌套过深。")
    for child in _children(box):
        if child.kind == b"meta":
            layout = _meta_layout(child)
            if layout:
                _check(not layout[4], "不支持轨道或media层ContentIdentifier；不能安全保留配对标识。")
        elif child.kind in (b"trak", b"mdia", b"udta"):
            _reject_unhandled_track_pairing(child, depth + 1)


def _load(path, cancel_event=None):
    path = Path(path)
    _cancel(cancel_event)
    signature = _signature(path)
    with path.open("rb") as stream:
        top = _top(stream, signature[0])
        _check(not any(b.kind in {b"moof", b"mfra", b"sidx"} for b in top), "不支持碎片化 MOV。")
        moov_pos = _one(top, b"moov")
        _check(moov_pos.size <= MAX_MOOV, "MOV 索引超出安全大小限制。")
        stream.seek(moov_pos.start)
        moov = _Box(b"moov", moov_pos.start, moov_pos.size, moov_pos.header, stream.read(moov_pos.size))
        children = _children(moov)
        _check(not any(b.kind in {b"cmov", b"mvex"} for b in children), "不支持压缩或碎片化 MOV 索引。")
        scale, duration = _clock(_one(children, b"mvhd"))
        mdats = [(b.start + b.header, b.start + b.size) for b in top if b.kind == b"mdat"]
        _check(mdats, "MOV 没有 mdat。")
        identifiers = []
        for parent in [children] + [_children(b) for b in children if b.kind == b"udta"]:
            for meta in (b for b in parent if b.kind == b"meta"):
                layout = _meta_layout(meta)
                if layout:
                    identifiers.extend(layout[-1])
        _check(len(set(identifiers)) <= 1, "MOV 中存在冲突的 ContentIdentifier。")
        tracks = []
        for track in (b for b in children if b.kind == b"trak"):
            _cancel(cancel_event)
            _reject_unhandled_track_pairing(track)
            tc = _children(track)
            tkhd = _one(tc, b"tkhd").payload
            _check(len(tkhd) >= 24 and tkhd[0] in (0, 1), "MOV track header 不支持。")
            identifier = _u32(tkhd, 12 if tkhd[0] == 0 else 20)
            _check(identifier > 0, "MOV track ID 无效。")
            mdia = _children(_one(tc, b"mdia"))
            hdlr = _one(mdia, b"hdlr").payload
            _check(len(hdlr) >= 12, "MOV handler 截断。")
            handler = hdlr[8:12]
            ts, td = _clock(_one(mdia, b"mdhd"))
            minf = _children(_one(mdia, b"minf"))
            dref = _one(_children(_one(minf, b"dinf")), b"dref").payload
            refs = _boxes(dref, 8)
            # QTFF permits a self-contained Macintosh alias as well as URL.
            # Require v0/self-reference-only with no external alias/URL data.
            _check(dref[:8] == b"\0\0\0\0\0\0\0\1" and len(refs) == 1 and refs[0].kind in (b"url ", b"alis")
                   and refs[0].payload == b"\0\0\0\1", "不支持外部 MOV 媒体引用。")
            stbl = _one(minf, b"stbl")
            stsd = _one(_children(stbl), b"stsd").payload
            entries = _boxes(stsd, 8)
            _check(stsd[:4] == b"\0" * 4 and len(entries) == _u32(stsd, 4) and entries, "MOV stsd 无效。")
            _check(all(e.kind not in {b"encv", b"enca"} and len(e.payload) >= 8 and e.payload[6:8] == b"\0\1" for e in entries),
                   "不支持加密或外部样本描述。")
            samples = _sample_ranges(stbl, entries, mdats)
            times, visible_times = _presentation_times(track, stbl, ts, len(samples), scale)
            keys, marker_ids, marker_times, metadata_samples = [], set(), [], []
            if handler == b"meta":
                _check(len(entries) == 1 and entries[0].kind == b"mebx", "不支持多描述或非mebx metadata轨道。")
                keys, marker_ids = _metadata_layout(entries[0])
                _check(not any(_one(_children(key), b"keyd").payload == b"mdta" + CONTENT_KEY
                               for key in keys if key.kind != b"\0" * 4),
                       "不支持时序ContentIdentifier；不能安全保留配对标识。")
                _check(sum(n for _, n in samples) <= MAX_METADATA, "MOV timed metadata 超出安全大小限制。")
                for (offset, size), when in zip(samples, times):
                    _cancel(cancel_event)
                    stream.seek(offset)
                    payload = stream.read(size)
                    atoms = _boxes(payload)
                    _check(not any(b.kind == b"\xff" * 4 for b in atoms), "不支持扩展 timed metadata sample。")
                    markers = [b for b in atoms if b.kind in marker_ids]
                    _check(len(markers) <= 1 and all(len(b.payload) == 1 for b in markers), "still-image-time 样本无效。")
                    _check(not markers or when in visible_times, "still-image-time 样本被edit list排除。")
                    marker_times.extend([when] * len(markers))
                    metadata_samples.append(payload)
            else:
                _check(STILL_KEY not in stsd and CONTENT_KEY not in stsd, "其他轨道包含未知 Live Photo metadata。")
            tracks.append(_Track(track, identifier, handler, ts, td, stbl, entries, samples, times, visible_times,
                                 marker_ids, keys, marker_times, metadata_samples))
    _check(tracks and len({t.identifier for t in tracks}) == len(tracks), "MOV track ID 缺失或重复。")
    _check(sum(t.handler == b"vide" for t in tracks) == 1, "仅支持单视频轨道 MOV。")
    _check(sum(len(t.marker_times) for t in tracks) <= 1, "MOV 包含多个 still-image-time 标记。")
    for t in tracks:
        _check(not t.marker_ids or len(t.marker_times) == 1, "still-image-time key 没有唯一有效样本。")
    _check(signature == _signature(path), "读取期间 MOV 源文件已更改。")
    return _Movie(path, signature, top, moov, children, scale, duration, tracks, identifiers[0] if identifiers else None)


def _info(movie, digests=()):
    times = [time for t in movie.tracks for time in t.marker_times]
    return LivePhotoMovieInfo(movie.asset_identifier, times[0] if times else None,
                             Fraction(movie.duration, movie.scale), True, None,
                             len(times), len(movie.tracks), digests)


def inspect_live_photo_movie(path, *, cancel_event=None):
    try:
        return _info(_load(path, cancel_event))
    except LivePhotoMovieError as error:
        return LivePhotoMovieInfo(unsupported_reason=str(error))


def video_presentation_times(path, *, cancel_event=None):
    movie = _load(path, cancel_event)
    return tuple(sorted(t for track in movie.tracks if track.handler == b"vide" for t in track.visible_times
                        if 0 <= t < Fraction(movie.duration, movie.scale)))


def media_payload_digests(path, *, cancel_event=None):
    """Hashes encoded samples in decode order, independently for audio/video."""
    movie = _load(path, cancel_event)
    result = []
    with Path(path).open("rb") as stream:
        for track in movie.tracks:
            if track.handler not in (b"vide", b"soun"):
                continue
            digest = hashlib.sha256()
            for offset, size in track.samples:
                _cancel(cancel_event)
                stream.seek(offset)
                remaining = size
                while remaining:
                    _cancel(cancel_event)
                    data = stream.read(min(1024 * 1024, remaining))
                    _check(data, "MOV 样本读取截断。")
                    digest.update(data)
                    remaining -= len(data)
            result.append((track.identifier, track.handler.decode("ascii"), digest.hexdigest()))
    _check(movie.signature == _signature(Path(path)), "校验期间 MOV 文件已更改。")
    return tuple(result)


def verify_movie_payload_preserved(source, destination, *, cancel_event=None):
    before = _load(source, cancel_event)
    after = _load(destination, cancel_event)
    _check((before.scale, before.duration) == (after.scale, after.duration), "MOV movie 时钟被更改。")
    for original in before.tracks:
        if original.handler not in (b"vide", b"soun"):
            continue
        matches = [t for t in after.tracks if t.identifier == original.identifier]
        _check(len(matches) == 1 and matches[0].box.data == original.box.data,
               "原音视频轨道索引、旋转或其他属性被更改。")
    a = media_payload_digests(source, cancel_event=cancel_event)
    b = media_payload_digests(destination, cancel_event=cancel_event)
    _check(a == b, "MOV 原音视频编码样本被更改。")
    return b


def _rewrite_meta(box, identifier):
    layout = _meta_layout(box)
    if layout is None:
        return box.data, False
    prefix, children, keys, values, ids, _ = layout
    _check(len(ids) <= 1, "同一 metadata 容器包含重复 ContentIdentifier key。")
    if not ids:
        keys.append(_Box(b"mdta", 0, 0, 8, _atom(b"mdta", CONTENT_KEY)))
        index = len(keys)
    else:
        index = next(iter(ids))
    value = _atom(_pack(index), _atom(b"data", _pack(1) + _pack(0) + identifier.encode("utf-8")))
    new_values = [b.data for b in values if int.from_bytes(b.kind, "big") != index] + [value]
    replacements = {b"keys": _full(b"keys", _pack(len(keys)) + b"".join(k.data for k in keys)),
                    b"ilst": _atom(b"ilst", b"".join(new_values))}
    return _atom(b"meta", prefix + b"".join(replacements.get(b.kind, b.data) for b in children)), True


def _new_meta(identifier):
    hdlr = _full(b"hdlr", b"\0" * 4 + b"mdta" + b"\0" * 12 + b"\0")
    keys = _full(b"keys", _pack(1) + _atom(b"mdta", CONTENT_KEY))
    value = _atom(_pack(1), _atom(b"data", _pack(1) + _pack(0) + identifier.encode("utf-8")))
    return _atom(b"meta", hdlr + keys + _atom(b"ilst", value))


def _rewrite_global_metadata(children, identifier):
    result = []
    updated = False
    for box in children:
        if box.kind == b"meta":
            raw, changed = _rewrite_meta(box, identifier)
            result.append(raw)
            updated |= changed
        elif box.kind == b"udta":
            output = []
            for item in _children(box):
                if item.kind == b"meta":
                    raw, changed = _rewrite_meta(item, identifier)
                    output.append(raw)
                    updated |= changed
                else:
                    output.append(item.data)
            result.append(_atom(b"udta", b"".join(output)))
        else:
            result.append(box.data)
    if not updated:
        result.append(_new_meta(identifier))
    return result


def _sample_tables(payloads, offset):
    return {
        b"stsc": _full(b"stsc", _pack(1) + struct.pack(">III", 1, len(payloads), 1)),
        b"stsz": _full(b"stsz", _pack(0) + _pack(len(payloads)) + b"".join(_pack(len(p)) for p in payloads)),
        b"stco": _full(b"co64", _pack(1) + struct.pack(">Q", offset)),
        b"co64": _full(b"co64", _pack(1) + struct.pack(">Q", offset)),
    }


def _canonical_metadata_track(track):
    """Normalize only the exact old test-build mebx padding; preserve samples."""
    entry = track.sample_entries[0]
    if entry.payload[8:16] != b"\0" * 8:
        return track.box.data
    entry = _atom(b"mebx", entry.payload[:8] + b"".join(b.data for b in _metadata_extensions(entry)))
    stbl = _replace(track.stbl, {b"stsd": _full(b"stsd", _pack(1) + entry)})
    mdia = _one(_children(track.box), b"mdia")
    minf = _one(_children(mdia), b"minf")
    return _replace(track.box, {b"mdia": _replace(mdia, {b"minf": _replace(minf, {b"stbl": stbl})})})


def _strip_marker(track, offset):
    entries = _metadata_extensions(track.sample_entries[0])
    keys = b"".join(key.data for key in track.keys if key.kind not in track.marker_ids)
    entry = _atom(b"mebx", track.sample_entries[0].payload[:8] + b"".join(
        _atom(b"keys", keys) if b.kind == b"keys" else b.data for b in entries))
    payloads = [b"".join(b.data for b in _boxes(p) if b.kind not in track.marker_ids)
                or _atom(b"\0" * 4) for p in track.metadata_samples]
    replacements = _sample_tables(payloads, offset)
    replacements[b"stsd"] = _full(b"stsd", _pack(1) + entry)
    stbl = _replace(track.stbl, replacements)
    mdia = _one(_children(track.box), b"mdia")
    minf = _one(_children(mdia), b"minf")
    return _replace(track.box, {b"mdia": _replace(mdia, {b"minf": _replace(minf, {b"stbl": stbl})})}), b"".join(payloads)


def _new_track(identifier, movie, when, offset, tref=b""):
    scale = math.lcm(movie.scale, when.denominator)
    _check(scale <= U32, "所选帧时间无法由支持的32位metadata时间基精确表示。")
    delay = int(when * scale)
    _check(delay < U32, "所选帧超过metadata单样本时长范围。")
    payloads = ([_atom(b"\0" * 4)] if delay else []) + [_atom(_pack(1), b"\xff")]
    ticks = ([delay] if delay else []) + [1]
    total = delay + 1
    movie_duration = (total * movie.scale + scale - 1) // scale
    _check(movie_duration <= movie.duration, "封面帧标记超出视频播放时长。")
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = _full(b"tkhd", struct.pack(">IIIII", 0, 0, identifier, 0, movie_duration)
                 + b"\0" * 16 + matrix + b"\0" * 8, flags=3)
    mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, scale, total, 0, 0))
    hdlr = _full(b"hdlr", b"\0" * 4 + b"meta" + b"\0" * 12 + b"Live Photo Metadata\0")
    declaration = _atom(b"keyd", b"mdta" + STILL_KEY) + _atom(b"dtyp", struct.pack(">II", 0, 65))
    entry = _atom(b"mebx", b"\0" * 6 + b"\0\1" + _atom(b"keys", _atom(_pack(1), declaration)))
    tables = _sample_tables(payloads, offset)
    stbl = _atom(b"stbl", _full(b"stsd", _pack(1) + entry)
                 + _full(b"stts", _pack(len(ticks)) + b"".join(struct.pack(">II", 1, tick) for tick in ticks))
                 + tables[b"stsc"] + tables[b"stsz"] + tables[b"co64"])
    # QTFF generic media header, graphicsmode=copy, white opcolor, no balance.
    gmhd = _atom(b"gmhd", _full(b"gmin", struct.pack(">HHHHHH", 0, 0xffff, 0xffff, 0xffff, 0, 0)))
    dinf = _atom(b"dinf", _full(b"dref", _pack(1) + _full(b"url ", flags=1)))
    mdia = _atom(b"mdia", mdhd + hdlr + _atom(b"minf", gmhd + dinf + stbl))
    return _atom(b"trak", tkhd + tref + mdia), b"".join(payloads)


def rewrite_live_photo_movie(source, destination, asset_identifier, photo_time: Fraction, *, cancel_event=None):
    """Write a new stage file only. Caller owns the later image+movie commit."""
    source, destination = Path(source), Path(destination)
    _check(source.resolve() != destination.resolve() and not destination.exists(), "MOV 输出必须为不存在的新文件。")
    _check(isinstance(asset_identifier, str) and asset_identifier and "\0" not in asset_identifier
           and len(asset_identifier.encode("utf-8")) <= 4096, "Live Photo 配对标识无效。")
    _check(isinstance(photo_time, Fraction), "封面时间必须是精确 Fraction，不能使用浮点估算。")
    movie = _load(source, cancel_event)
    duration = Fraction(movie.duration, movie.scale)
    _check(0 <= photo_time < duration, "封面帧时间超出视频时长。")
    video_times = [time for t in movie.tracks if t.handler == b"vide" for time in t.visible_times]
    _check(photo_time in video_times, "封面时间不对应视频实际呈现帧。")
    _check(video_times.count(photo_time) == 1, "多个视频帧具有相同呈现时间，无法唯一确定封面。")
    marker_tracks = [t for t in movie.tracks if t.marker_ids]
    _check(len(marker_tracks) <= 1, "多条 still-image-time 轨道存在歧义。")
    replace_track = None
    if marker_tracks and all(k.kind in marker_tracks[0].marker_ids or k.kind == b"\0" * 4 for k in marker_tracks[0].keys):
        replace_track = marker_tracks[0]
        _check(all(all(b.kind in replace_track.marker_ids or b.kind == b"\0" * 4 for b in _boxes(p))
                   for p in replace_track.metadata_samples), "原标记轨道含未知样本，不能安全替换。")
    next_id = replace_track.identifier if replace_track else max(t.identifier for t in movie.tracks) + 1
    _check(next_id < U32, "MOV track ID 用尽。")
    appended = bytearray()
    # Old test-build files may already have a marker removed from another
    # metadata track. Canonicalize its description too, without relocating or
    # editing any of that track's sample values/times or other declarations.
    track_updates = {t.identifier: _canonical_metadata_track(t) for t in movie.tracks if t.handler == b"meta"}
    base_offset = movie.signature[0] + 8
    for track in marker_tracks:
        if track is not replace_track:
            allowed = {b"stsd", b"stts", b"ctts", b"stsc", b"stsz", b"stco", b"co64", b"stss", b"sdtp"}
            _check(all(b.kind in allowed for b in _children(track.stbl)), "混合metadata轨道有尚未支持的采样扩展。")
            raw, payload = _strip_marker(track, base_offset + len(appended))
            track_updates[track.identifier] = raw
            appended.extend(payload)
    tref = b""
    if marker_tracks:
        reference = _one(_children(marker_tracks[0].box), b"tref", optional=True)
        tref = reference.data if reference else b""
    new_track, payload = _new_track(next_id, movie, photo_time, base_offset + len(appended), tref)
    appended.extend(payload)
    children = []
    for box in movie.children:
        if box.kind == b"trak":
            track = next(t for t in movie.tracks if t.box is box)
            if track is replace_track:
                children.append(new_track)
            else:
                children.append(track_updates.get(track.identifier, box.data))
        elif box.kind == b"mvhd":
            data = bytearray(box.payload)
            _check(len(data) >= 100, "MOV movie header 截断。")
            data[-4:] = _pack(max(_u32(data, len(data) - 4), next_id + 1))
            children.append(_atom(b"mvhd", bytes(data)))
        else:
            children.append(box.data)
    if not replace_track:
        children.append(new_track)
    rewritten = _atom(b"moov", b"".join(_rewrite_global_metadata(_boxes(b"".join(children)), asset_identifier)))
    # Reuse the active movie slot on later saves; no accumulating dead moov copies.
    # A shorter previous index left a padding free immediately after moov.
    # Coalesce that padding too, so alternating time=0/nonzero cannot accumulate
    # dead movie indexes. Original media offsets and total prefix size stay fixed.
    slot_end = movie.moov.start + movie.moov.size
    padding_starts = set()
    for top in movie.top:
        if (top.start == slot_end and top.kind == b"free"
                and slot_end - movie.moov.start + top.size <= MAX_MOOV):
            padding_starts.add(top.start)
            slot_end += top.size
    spare = slot_end - movie.moov.start - len(rewritten)
    in_place = spare == 0 or spare >= 8
    moov_patch = rewritten + (_atom(b"free", b"\0" * (spare - 8)) if spare >= 8 else b"") if in_place else None
    created = False
    try:
        _cancel(cancel_event)
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            created = True
            for top in movie.top:
                _cancel(cancel_event)
                if in_place and top.start in padding_starts:
                    continue
                incoming.seek(top.start)
                if top.kind == b"moov" and in_place:
                    outgoing.write(moov_patch)
                    continue
                remaining = top.size
                first = True
                while remaining:
                    _cancel(cancel_event)
                    block = incoming.read(min(1024 * 1024, remaining))
                    _check(block, "MOV 复制过程中截断。")
                    if first:
                        if top.kind == b"moov":
                            block = block[:4] + b"free" + block[8:]
                        elif block[:4] == b"\0" * 4:
                            block = _pack(top.size) + block[4:]
                        first = False
                    outgoing.write(block)
                    remaining -= len(block)
            outgoing.write(_atom(b"mdat", bytes(appended)))
            if not in_place:
                outgoing.write(rewritten)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        _check(_signature(source) == movie.signature, "保存期间 MOV 源文件已更改。")
        check = _load(destination, cancel_event)
        result = _info(check)
        _check(result.asset_identifier == asset_identifier and result.photo_time == photo_time and result.marker_count == 1,
               "写入后的 Live Photo 标识或封面时间校验失败。")
        digests = verify_movie_payload_preserved(source, destination, cancel_event=cancel_event)
        _cancel(cancel_event)
        return _info(check, digests)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
