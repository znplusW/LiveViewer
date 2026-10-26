"""Move a Live Photo marker using the original track's native shell.

No media encoder, new handler template, or identifier writer is used here.
The original ID is retained for a separate, independently checked ID edit.
This module verifies resource structure; device import is a separate check.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import os
from pathlib import Path
import struct

from . import live_photo_movie as m


def _fraction(value):
    return None if value is None else {"numerator": value.numerator, "denominator": value.denominator}

def _hash_region(path, start, size, cancel_event):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        stream.seek(start)
        while size:
            m._cancel(cancel_event)
            block = stream.read(min(size, 1024 * 1024))
            m._check(block, "MOV 校验读取截断。")
            digest.update(block)
            size -= len(block)
    return digest.hexdigest()

def _hash(path, cancel_event):
    signature = m._signature(path)
    result = _hash_region(path, 0, signature[0], cancel_event)
    m._check(signature == m._signature(path), "MOV 哈希期间文件已变动。")
    return result

def _track_outside_sample_table(track):
    mdia = m._one(m._children(track.box), b"mdia")
    minf = m._one(m._children(mdia), b"minf")
    return m._replace(track.box, {b"mdia": m._replace(mdia, {
        b"minf": m._replace(minf, {b"stbl": b""})})})

def _global_non_identifier_fields(loaded):
    """Retain actual key definitions and data, without relying on ID indexes."""
    result = []
    for location, boxes in [("moov", loaded.children)] + [
            (f"udta-{i}", m._children(b))
            for i, b in enumerate(loaded.children) if b.kind == b"udta"]:
        for j, box in enumerate(b for b in boxes if b.kind == b"meta"):
            layout = m._meta_layout(box)
            if layout is None:
                result.append((location, j, "opaque", box.data))
                continue
            _, _, keys, values, ids, _ = layout
            kept = tuple((i + 1, key.data) for i, key in enumerate(keys) if i + 1 not in ids)
            data = tuple(value.data for value in values if int.from_bytes(value.kind, "big") not in ids)
            if kept or data:
                result.append((location, j, kept, data))
    return result

def _verify_preserved(before, after, method, cancel_event):
    m._check((before.scale, before.duration) == (after.scale, after.duration), "原 MOV 时钟发生改变。")
    m._check(_global_non_identifier_fields(before) == _global_non_identifier_fields(after),
                 "非关联标识的全局元数据发生改变。")
    # Entire old mdat payloads stay at the same offsets. This also protects
    # opaque samples in auxiliary tracks, not just conventional audio/video.
    count = 0
    for box in before.top:
        if box.kind != b"mdat":
            continue
        start, size = box.start + box.header, box.size - box.header
        m._check(_hash_region(before.path, start, size, cancel_event)
                     == _hash_region(after.path, start, size, cancel_event),
                     "原 mdat 样本字节发生改变。")
        count += 1
    after_tracks = {track.identifier: track for track in after.tracks}
    preserved = []
    auxiliary = []
    for old in before.tracks:
        m._cancel(cancel_event)
        new = after_tracks.get(old.identifier)
        m._check(new is not None, "原轨道丢失。")
        if method == "identifier_only" or old.handler != b"meta":
            m._check(old.box.data == new.box.data, "对照轨道或非metadata轨道索引发生改变。")
        elif not old.marker_ids:
            m._check(m._canonical_metadata_track(old) == new.box.data,
                         "无标记metadata轨道除旧描述规范化外发生改变。")
        elif any(key.kind not in old.marker_ids and key.kind != b"\0" * 4 for key in old.keys):
            m._check(not new.marker_ids and old.times == new.times
                         and old.visible_times == new.visible_times,
                         "混合metadata轨道其他样本时间发生改变。")
            m._check(_track_outside_sample_table(old) == _track_outside_sample_table(new),
                         "混合metadata轨道非采样表属性发生改变。")
            ignored = {b"stsd", b"stsc", b"stsz", b"stco", b"co64"}
            m._check([b.data for b in m._children(old.stbl) if b.kind not in ignored]
                         == [b.data for b in m._children(new.stbl) if b.kind not in ignored],
                         "混合metadata的原时间表或其他采样属性发生改变。")
            m._check([k.data for k in old.keys if k.kind not in old.marker_ids]
                         == [k.data for k in new.keys], "混合metadata非标记声明发生改变。")
            expected = [b"".join(b.data for b in m._boxes(p) if b.kind not in old.marker_ids)
                        or m._atom(b"\0" * 4) for p in old.metadata_samples]
            m._check(expected == new.metadata_samples, "混合metadata非标记样本发生改变。")
        else:
            # A pure marker track is intentionally replaced at the same ID.
            old_ref = m._one(m._children(old.box), b"tref", optional=True)
            new_ref = m._one(m._children(new.box), b"tref", optional=True)
            m._check((old_ref.data if old_ref else None) == (new_ref.data if new_ref else None),
                         "标记轨道关联发生改变。")
            continue
        preserved.append(old.identifier)
        if old.handler not in (b"vide", b"soun", b"meta"):
            auxiliary.append(old.identifier)
    m._check(before.signature == m._signature(before.path)
                 and after.signature == m._signature(after.path), "验证期间 MOV 文件已变动。")
    return {
        "all_original_mdat_payloads_preserved": True,
        "original_mdat_count": count,
        "non_marker_sample_bytes_and_timing_preserved": True,
        "non_metadata_tracks_byte_identical": True,
        "non_identifier_global_metadata_preserved": True,
        "preserved_track_ids": preserved,
        "auxiliary_track_ids": auxiliary,
    }


def _field(data, offset, width, value):
    m._check(0 <= value < 1 << (8 * width) and offset + width <= len(data),
             "原轨道时间字段不能精确容纳新值。")
    changed = bytearray(data)
    changed[offset:offset + width] = value.to_bytes(width, 'big')
    return bytes(changed)


def _marker_sample(track):
    deltas = [delta for count, delta in m._table(m._one(m._children(track.stbl), b'stts'), 2)
              for _ in range(count)]
    found = []
    for index, payload in enumerate(track.metadata_samples):
        atoms = [box for box in m._boxes(payload) if box.kind in track.marker_ids]
        if atoms:
            m._check(len(atoms) == 1 and atoms[0].payload == b'\xff',
                     "原标记必须是唯一 int8 -1 样本。")
            found.append((index, atoms[0].data, deltas[index]))
    m._check(len(found) == 1, "原轨道缺少唯一有效标记。")
    return found[0]


def _edit(track, delay, duration):
    old = m._one(m._children(track.box), b'edts', optional=True)
    version = 0
    if old:
        children = m._children(old)
        m._check(len(children) == 1 and children[0].kind == b'elst',
                 "原编辑列表含未知扩展，不能安全调整。")
        version = children[0].payload[0]
    m._check(version in (0, 1), "编辑列表版本不支持。")
    edits = ([(delay, -1)] if delay else []) + [(duration, 0)]
    width = 64 if version else 32
    m._check(all(0 < length < 1 << width for length, _ in edits), "编辑列表时长超出原版本范围。")
    content = m._pack(len(edits)) + b''.join(
        struct.pack('>Qqhh' if version else '>Iihh', length, media_start, 1, 0)
        for length, media_start in edits)
    return m._atom(b'edts', m._full(b'elst', content, version=version))


def _clone_marker(track, identifier, loaded, when, offset, marker, delta):
    """Retain shell and original marker declaration; replace only time/index data."""
    movie_delay = when * loaded.scale
    m._check(movie_delay.denominator == 1,
             "所选精确 PTS 不能由原 movie 时钟表示，拒绝近似。")
    delay = int(movie_delay)
    duration = Fraction(delta, track.scale)
    # The sample duration/media scale stay exact. The edit envelope uses movie
    # ticks and may extend to the next tick, as track durations require.
    edit_ticks = (delta * loaded.scale + track.scale - 1) // track.scale
    m._check(when + duration <= Fraction(loaded.duration, loaded.scale)
             and delay + edit_ticks <= loaded.duration, "原标记持续时间在新位置超出影片结尾。")
    tc = m._children(track.box)
    tkhd = m._one(tc, b'tkhd')
    m._check(tkhd.payload[0] in (0, 1), "原 tkhd 版本不支持。")
    id_at = 20 if tkhd.payload[0] else 12
    duration_at, duration_width = (28, 8) if tkhd.payload[0] else (20, 4)
    new_tkhd = _field(tkhd.payload, id_at, 4, identifier)
    new_tkhd = _field(new_tkhd, duration_at, duration_width, delay + edit_ticks)
    mdia = m._one(tc, b'mdia')
    mdhd = m._one(m._children(mdia), b'mdhd')
    mdhd_at, mdhd_width = (24, 8) if mdhd.payload[0] else (16, 4)
    new_mdhd = _field(mdhd.payload, mdhd_at, mdhd_width, delta)
    # Preserve all sample-entry extensions, but only the existing marker key.
    entry = track.sample_entries[0]
    key_bytes = b''.join(key.data for key in track.keys if key.kind in track.marker_ids)
    m._check(key_bytes, "原标记声明不存在。")
    new_entry = m._atom(b'mebx', entry.payload[:8] + b''.join(
        m._atom(b'keys', key_bytes) if box.kind == b'keys' else box.data
        for box in m._metadata_extensions(entry)))
    original_tables = m._children(track.stbl)
    allowed = {b'stsd', b'stts', b'ctts', b'stsc', b'stsz', b'stco', b'co64'}
    m._check(all(box.kind in allowed for box in original_tables),
             "原标记轨含依赖样本编号的未知扩展，不能安全克隆。")
    co_kind = next(box.kind for box in original_tables if box.kind in (b'stco', b'co64'))
    if offset > m.U32:
        co_kind = b'co64'
    offsets = m._full(co_kind, m._pack(1) + offset.to_bytes(8 if co_kind == b'co64' else 4, 'big'))
    replacements = {
        b'stsd': m._full(b'stsd', m._pack(1) + new_entry),
        b'stts': m._full(b'stts', m._pack(1) + struct.pack('>II', 1, delta)),
        b'ctts': b'',
        b'stsc': m._full(b'stsc', m._pack(1) + struct.pack('>III', 1, 1, 1)),
        b'stsz': m._full(b'stsz', m._pack(0) + m._pack(1) + m._pack(len(marker))),
        b'stco': offsets, b'co64': offsets,
    }
    stbl = m._replace(track.stbl, replacements)
    minf = m._one(m._children(mdia), b'minf')
    new_mdia = m._replace(mdia, {
        b'mdhd': m._atom(b'mdhd', new_mdhd),
        b'minf': m._replace(minf, {b'stbl': stbl}),
    })
    edit = _edit(track, delay, edit_ticks)
    replacements = {b'tkhd': m._atom(b'tkhd', new_tkhd), b'edts': edit, b'mdia': new_mdia}
    raw = m._replace(track.box, replacements)
    if not any(box.kind == b'edts' for box in tc):
        # Add the edit directly after tkhd, leaving remaining shell ordering.
        raw = m._atom(b'trak', b''.join(replacements.get(box.kind, box.data)
                                      + (edit if box.kind == b'tkhd' else b'') for box in tc))
    return raw, duration, Fraction(edit_ticks, loaded.scale)


def _copy_with_movie(loaded, destination, rewritten, appended, tail_moov, cancel_event):
    created = False
    try:
        with loaded.path.open('rb') as incoming, destination.open('xb') as outgoing:
            created = True
            for box in loaded.top:
                m._cancel(cancel_event)
                if box.kind == b'moov' and tail_moov:
                    continue
                incoming.seek(box.start)
                remaining, first = box.size, True
                while remaining:
                    m._cancel(cancel_event)
                    data = incoming.read(min(remaining, 1024 * 1024))
                    m._check(data, "复制原 MOV 时读取截断。")
                    if first:
                        if box.kind == b'moov':
                            data = data[:4] + b'free' + data[8:]
                        elif data[:4] == b'\0' * 4:
                            data = m._pack(box.size) + data[4:]
                        first = False
                    outgoing.write(data)
                    remaining -= len(data)
            outgoing.write(m._atom(b'mdat', appended))
            outgoing.write(rewritten)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        m._check(loaded.signature == m._signature(loaded.path), "保存期间原 MOV 已更改。")
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def rewrite_original_marker(source, destination, photo_time: Fraction, cancel_event=None):
    """Create new MOV at exact PTS, preserving ID and original marker shell.

    Pure-marker tracks retain their ID. Mixed metadata keeps its old ID and
    all non-marker timing/data; a clone holding the original marker key gets
    a fresh track ID. Only an existing, uniquely supported marker is accepted.
    """
    source, destination = Path(source), Path(destination)
    m._check(os.path.normcase(os.path.abspath(source)) != os.path.normcase(os.path.abspath(destination))
             and not destination.exists() and not destination.is_symlink(), "输出必须是不存在的新 MOV。")
    m._check(isinstance(photo_time, Fraction), "标记时间必须为精确 Fraction。")
    loaded = m._load(source, cancel_event)
    m._check(loaded.asset_identifier, "原 MOV 缺少配对标识。")
    tracks = [track for track in loaded.tracks if track.marker_ids]
    m._check(len(tracks) == 1, "需要原 MOV 的唯一有效 still-image-time 轨道。")
    original = tracks[0]
    visible = [time for track in loaded.tracks if track.handler == b'vide' for time in track.visible_times]
    m._check(0 <= photo_time < Fraction(loaded.duration, loaded.scale)
             and visible.count(photo_time) == 1, "所选时间不是唯一有效视频呈现帧。")
    marker_index, marker, delta = _marker_sample(original)
    mixed = any(key.kind not in original.marker_ids and key.kind != b'\0' * 4 for key in original.keys)
    if not mixed:
        m._check(all(all(box.kind in original.marker_ids or box.kind == b'\0' * 4
                         for box in m._boxes(payload)) for payload in original.metadata_samples),
                 "纯标记轨包含未声明样本。")
    identifier = max(track.identifier for track in loaded.tracks) + 1 if mixed else original.identifier
    m._check(identifier < m.U32, "轨道 ID 已用尽。")
    source_hash = _hash(source, cancel_event)
    m._check(loaded.signature == m._signature(source), "处理前原 MOV 已变动。")
    # A trailing index can be replaced directly in the new file: no dead moov
    # body remains. Other layouts retain prefix offsets by replacing old moov
    # with free before appending a single active moov.
    tail_moov = loaded.top[-1].kind == b'moov'
    append_at = loaded.moov.start if tail_moov else loaded.signature[0]
    payload = bytearray()
    original_update = None
    if mixed:
        allowed = {b'stsd', b'stts', b'ctts', b'stsc', b'stsz', b'stco', b'co64'}
        m._check(all(box.kind in allowed for box in m._children(original.stbl)),
                 "原混合metadata采样扩展尚不支持。")
        original_update, stripped = m._strip_marker(original, append_at + 8)
        payload.extend(stripped)
    clone, duration, edit_duration = _clone_marker(
        original, identifier, loaded, photo_time, append_at + 8 + len(payload), marker, delta)
    payload.extend(marker)
    children = []
    for box in loaded.children:
        if box is original.box:
            children.append(original_update if mixed else clone)
        elif box.kind == b'mvhd' and mixed:
            data = box.payload
            children.append(m._atom(b'mvhd', _field(data, len(data)-4, 4,
                            max(m._u32(data, len(data)-4), identifier+1))))
        else:
            children.append(box.data)
    if mixed:
        children.append(clone)
    rewritten = m._atom(b'moov', b''.join(children))
    created = False
    try:
        _copy_with_movie(loaded, destination, rewritten, bytes(payload), tail_moov, cancel_event)
        created = True
        after = m._load(destination, cancel_event)
        info = m._info(after)
        m._check(info.asset_identifier == loaded.asset_identifier and info.photo_time == photo_time
                 and info.marker_count == 1, "新文件配对标识或精确标记时间校验失败。")
        verification = _verify_preserved(loaded, after, 'qtff', cancel_event)
        new = next(track for track in after.tracks if track.identifier == identifier)
        m._check(new.scale == original.scale and len(new.samples) == 1 and new.metadata_samples == [marker],
                 "原标记样本或时间基发生改变。")
        m._check(m._table(m._one(m._children(new.stbl), b'stts'), 2) == [(1, delta)],
                 "原标记持续时间发生改变。")
        for kind in (b'hdlr',):
            old_box = m._one(m._children(m._one(m._children(original.box), b'mdia')), kind)
            new_box = m._one(m._children(m._one(m._children(new.box), b'mdia')), kind)
            m._check(old_box.data == new_box.data, "原生 handler 未保留。")
        for kind in (b'tref',):
            old_box = m._one(m._children(original.box), kind, optional=True)
            new_box = m._one(m._children(new.box), kind, optional=True)
            m._check((old_box.data if old_box else None) == (new_box.data if new_box else None),
                     "原生轨道关联未保留。")
        m._check(m._one(m._children(original.box), b'tkhd').payload[:4]
                 == m._one(m._children(new.box), b'tkhd').payload[:4], "原 tkhd flags 未保留。")
        m._check([key.data for key in original.keys if key.kind in original.marker_ids]
                 == [key.data for key in new.keys], "原 marker key ID 或完整声明未保留。")
        output_hash = _hash(destination, cancel_event)
        m._check(loaded.signature == m._signature(source) and after.signature == m._signature(destination),
                 "最终校验期间文件已更改。")
        m._cancel(cancel_event)
        return {
            'schema': 'liveviewer.native-original-marker.v1',
            'method': 'original_marker_shell',
            'source_sha256': source_hash, 'output_sha256': output_hash,
            'asset_identifier': info.asset_identifier, 'identifier_changed': False,
            'source_time': _fraction(original.marker_times[0]), 'output_time': _fraction(photo_time),
            'source_marker_sample_index': marker_index,
            'marker_duration': _fraction(duration), 'edit_envelope_duration': _fraction(edit_duration),
            'marker_media_scale': original.scale, 'marker_duration_preserved': True,
            'source_marker_track': original.identifier, 'output_marker_track': identifier,
            'mixed_track_split': mixed, 'marker_count': 1, 'track_count': info.track_count,
            'native_handler_flags_tref_preserved': True,
            'marker_key_id_and_declaration_preserved': True,
            'trailing_moov_replaced_without_dead_index': tail_moov,
            'verification': verification, 'import_compatibility_verified': False,
        }
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
