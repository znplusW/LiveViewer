"""Synthetic BMFF fixtures, plus the repository's generated H.264/HEVC clips.

All fixture bytes below are generated here, not copied from personal media.
The builder deliberately does not import the writer's atom/table helpers.
"""
from fractions import Fraction
import hashlib
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from livegallery import live_photo_movie as movie
from livegallery.live_photo_native_movie import rewrite_original_marker
from livegallery.live_photo_movie import inspect_live_photo_movie, video_presentation_times


def box(kind, data=b"", extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(data) + 16) + data
    return struct.pack(">I4s", len(data) + 8, kind) + data


def full(kind, data=b"", version=0, flags=0):
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + data)


def integer(value):
    return struct.pack(">I", value)


def split_atoms(data, offset=0):
    atoms = []
    while offset < len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        head = 8
        if length == 1:
            length = struct.unpack_from(">Q", data, offset + 8)[0]
            head = 16
        if not length:
            length = len(data) - offset
        assert length >= head and offset + length <= len(data)
        atoms.append((kind, data[offset + head:offset + length], data[offset:offset + length], offset))
        offset += length
    assert offset == len(data)
    return atoms


MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
KEY = b"com.apple.quicktime.still-image-time"
ID_KEY = b"com.apple.quicktime.content.identifier"


def sample_entry(handler, metadata_keys=None, *, legacy_mebx=False, reference_index=1):
    header = b"\0" * 6 + struct.pack(">H", reference_index)
    if handler == b"meta":
        keys = b"".join(box(integer(i), box(b"keyd", b"mdta" + name)
                        + box(b"dtyp", struct.pack(">II", 0, dtype))) for i, name, dtype in metadata_keys)
        # QTFF standard sample description header is 16 bytes INCLUDING size
        # and format: only reserved[6]+data-reference-index[2] in the payload.
        return box(b"mebx", header + (b"\0" * 8 if legacy_mebx else b"") + box(b"keys", keys))
    return box(b"avc1" if handler == b"vide" else b"mp4a", header + b"synthetic-codec-description")


def track(track_id, handler, samples, offset, *, scale=10, deltas=None, co64=False,
          ctts=None, edit=None, metadata_keys=None, extended=False, dref_atom=None,
          legacy_mebx=False, reference_index=1, track_duration=1000):
    deltas = deltas or [1] * len(samples)
    duration = sum(deltas)
    tkhd = full(b"tkhd", struct.pack(">IIIII", 101, 102, track_id, 0, track_duration)
                + b"\0" * 16 + MATRIX + struct.pack(">II", 16 << 16, 16 << 16), flags=3)
    mdhd = full(b"mdhd", struct.pack(">IIIIHH", 101, 102, scale, duration, 0, 0))
    hdlr = full(b"hdlr", b"\0" * 4 + handler + b"\0" * 12 + b"Synthetic\0")
    stsd = full(b"stsd", integer(1) + sample_entry(handler, metadata_keys,
                                                legacy_mebx=legacy_mebx, reference_index=reference_index))
    stts = full(b"stts", integer(len(deltas)) + b"".join(struct.pack(">II", 1, n) for n in deltas))
    co = full(b"co64" if co64 else b"stco", integer(1) + struct.pack(">Q" if co64 else ">I", offset))
    stsz = full(b"stsz", integer(0) + integer(len(samples)) + b"".join(integer(len(s)) for s in samples))
    stsc = full(b"stsc", integer(1) + struct.pack(">III", 1, len(samples), 1))
    composition = full(b"ctts", integer(len(ctts)) + b"".join(struct.pack(">Ii", 1, n) for n in ctts), version=1) if ctts else b""
    stbl = box(b"stbl", stsd + stts + composition + stsc + stsz + co)
    dref = box(b"dinf", full(b"dref", integer(1) + full(b"url ", flags=1)) if dref_atom is None else dref_atom)
    mdia = box(b"mdia", mdhd + hdlr + box(b"minf", full(b"nmhd") + dref + stbl))
    edts = b""
    if edit:
        version, edits = edit
        elst = full(b"elst", integer(len(edits)) + b"".join(struct.pack(">Qqhh" if version else ">Iihh", d, t, 1, 0)
                                                              for d, t in edits), version=version)
        edts = box(b"edts", elst)
    return box(b"trak", tkhd + edts + mdia, extended)


def metadata(identifier, *, iso=False):
    return box(b"meta", (b"\0" * 4 if iso else b"")
               + full(b"hdlr", b"\0" * 4 + b"mdta" + b"\0" * 12 + b"\0")
               + full(b"keys", integer(2) + box(b"mdta", ID_KEY) + box(b"mdta", b"com.apple.quicktime.creationdate"))
               + box(b"ilst", box(integer(1), box(b"data", integer(1) + integer(0) + identifier.encode()))
                     + box(integer(2), box(b"data", integer(1) + integer(0) + b"2020-01-02T03:04:05+0800"))))


def make_movie(path, *, co64=False, extended=False, mixed=False, marker_only=False,
               ctts=None, edit=None, movie_scale=1000, movie_duration=1000, dual_identifier=None,
               video_scale=10, deltas=(2, 3, 5), front_moov=False, zero_mdat=False,
               extra_metadata_key=b"com.example.synthetic", dref_atom=None,
               legacy_mebx=False, reference_index=1, other_metadata_tracks=False):
    video = [b"video-sample-0", b"second-video-sample", b"third-video-sample"]
    audio = [b"audio-sample-0", b"audio-sample-1"]
    meta_samples = []
    if mixed:
        meta_samples = [box(integer(2), b"first\0"), box(integer(1), b"\xff") + box(integer(2), b"second\0")]
    elif marker_only:
        meta_samples = [box(integer(1), b"\xff")]
    auxiliary = ([box(integer(7), b"synthetic-a0"), box(integer(7), b"synthetic-a1")],
                 [box(integer(9), b"synthetic-b0"), box(integer(9), b"synthetic-b1")]) if other_metadata_tracks else ()
    ftyp = box(b"ftyp", b"qt  " + integer(0) + b"qt  ")
    mdat_payload = b"".join(video + audio + meta_samples + [sample for samples in auxiliary for sample in samples])
    mvhd = full(b"mvhd", struct.pack(">IIII", 101, 102, movie_scale, movie_duration)
                + struct.pack(">IHH", 0x10000, 0x100, 0) + b"\0" * 8 + MATRIX + b"\0" * 24 + integer(4))

    def movie(offset):
        tracks = track(1, b"vide", video, offset, deltas=list(deltas), scale=video_scale, ctts=ctts,
                       edit=edit, co64=co64, extended=extended, dref_atom=dref_atom, reference_index=reference_index,
                       track_duration=movie_duration)
        tracks += track(2, b"soun", audio, offset + sum(map(len, video)), co64=co64,
                        dref_atom=dref_atom, reference_index=reference_index)
        if meta_samples:
            keys = [(1, KEY, 65)] + ([(2, extra_metadata_key, 1)] if mixed else [])
            tracks += track(3, b"meta", meta_samples, offset + sum(map(len, video + audio)),
                            deltas=[2, 1] if mixed else [1], metadata_keys=keys, co64=co64,
                            dref_atom=dref_atom, legacy_mebx=legacy_mebx, reference_index=reference_index)
        extra_offset = offset + sum(map(len, video + audio + meta_samples))
        for index, samples in enumerate(auxiliary):
            key_id = (7, 9)[index]
            tracks += track(4 + index, b"meta", samples, extra_offset, deltas=[2 + index, 1],
                            metadata_keys=[(key_id, b"com.example.synthetic." + str(index).encode(), 1)],
                            co64=co64, dref_atom=dref_atom, reference_index=reference_index, legacy_mebx=legacy_mebx)
            extra_offset += sum(map(len, samples))
        # QuickTime moov/meta has no FullBox prefix; udta/meta follows the
        # commonly used ISO FullBox form. ExifTool parses these independently.
        tags = metadata("old-identifier") if dual_identifier is not None else b""
        if dual_identifier is not None:
            tags += box(b"udta", metadata(dual_identifier, iso=True))
        return box(b"moov", mvhd + tracks + tags, extended)

    mdat_header = 16 if extended else 8
    size = len(movie(0)) if front_moov else 0
    moov = movie(len(ftyp) + size + mdat_header)
    mdat = box(b"mdat", mdat_payload, extended)
    if zero_mdat:
        assert front_moov and not extended
        mdat = b"\0" * 4 + mdat[4:]
    path.write_bytes(ftyp + (moov + mdat if front_moov else mdat + moov))
    return video, audio


def child(data, kind):
    matches = [p for k, p, *_ in split_atoms(data) if k == kind]
    assert len(matches) == 1
    return matches[0]


def tracks(path):
    return [p for k, p, *_ in split_atoms(child(path.read_bytes(), b"moov")) if k == b"trak"]


def marked_track(path):
    found = []
    for raw in tracks(path):
        mdia = child(raw, b"mdia")
        if child(mdia, b"hdlr")[8:12] != b"meta":
            continue
        stbl = child(child(mdia, b"minf"), b"stbl")
        entry = split_atoms(child(stbl, b"stsd"), 8)[0]
        keys = child(entry[1][8:], b"keys")
        if any(child(p, b"keyd") == b"mdta" + KEY for _, p, *_ in split_atoms(keys)):
            found.append((raw, mdia, stbl, entry))
    assert len(found) == 1
    return found[0]


def replace_child(raw, kind, replacement):
    return b''.join(replacement if k == kind else entire for k,_,entire,_ in split_atoms(raw))


def native_source(path, *, mixed=True, auxiliaries=True, front=False, marker_id=7, clock=1000):
    """Independent synthetic native-style shell; no private payload bytes."""
    make_movie(path,mixed=mixed,marker_only=not mixed,other_metadata_tracks=auxiliaries,
               dual_identifier='old-identifier',front_moov=front,movie_scale=clock,movie_duration=clock,
               dref_atom=full(b'dref',integer(1)+full(b'alis',flags=1)))
    data=path.read_bytes()
    # Growing a front index would shift sample offsets. Native-style fixtures
    # here are tail-moov; front layout has separate tests using fixed sizes.
    assert not front
    top=split_atoms(data)
    raw=marked_track(path)[0]
    tkhd=bytearray(child(raw,b'tkhd'));tkhd[:4]=b'\0\0\0\x0f'
    tkhd[20:24]=integer(clock//5+1)
    mdia=child(raw,b'mdia')
    mdhd=bytearray(child(mdia,b'mdhd'))
    mdhd[12:20]=struct.pack('>II',clock,1)
    mdhd[20:22]=struct.pack('>H',21956)
    hdlr=full(b'hdlr',b'mhlrmetaappl'+integer(1)+integer(0)+b'\x13Core Media Metadata')
    minf=child(mdia,b'minf');stbl=child(minf,b'stbl')
    stsd=child(stbl,b'stsd');kind,entry,*_=split_atoms(stsd,8)[0]
    keys=child(entry[8:],b'keys')
    newkeys=b''.join(box(integer(marker_id),p) if child(p,b'keyd')==b'mdta'+KEY else whole
                     for k,p,whole,_ in split_atoms(keys))
    newentry=box(kind,entry[:8]+box(b'keys',newkeys))
    old_offset=struct.unpack_from('>I',child(stbl,b'stco'),8)[0]
    source_samples=movie._load(path).tracks[2].metadata_samples
    old_sample=source_samples[-1]
    marker_old=box(integer(1),b'\xff')
    marker_new=box(integer(marker_id),b'\xff')
    # Existing sample extent stays same length: just change the fixture key ID.
    sample_offset=old_offset+sum(map(len,source_samples[:-1]))
    data=data[:sample_offset]+old_sample.replace(marker_old,marker_new,1)+data[sample_offset+len(old_sample):]
    table={b'stsd':full(b'stsd',integer(1)+newentry),
           b'stts':full(b'stts',integer(1)+struct.pack('>II',1,1)),
           b'stsc':full(b'stsc',integer(1)+struct.pack('>III',1,1,1)),
           b'stsz':full(b'stsz',struct.pack('>III',0,1,len(old_sample))),
           b'stco':full(b'stco',integer(1)+integer(sample_offset))}
    newstbl=box(b'stbl',b''.join(table.get(k,whole) for k,p,whole,_ in split_atoms(stbl)))
    minf=replace_child(minf,b'stbl',newstbl)
    minf=full(b'hdlr',b'dhlralisappl'+b'\0'*8+b'\x13Core Media Data Hdlr')+minf
    mdia=replace_child(mdia,b'mdhd',box(b'mdhd',bytes(mdhd)))
    mdia=replace_child(mdia,b'hdlr',hdlr)
    mdia=replace_child(mdia,b'minf',box(b'minf',minf))
    edts=box(b'edts',full(b'elst',integer(2)+struct.pack('>IihhIihh',clock//5,-1,1,0,1,0,1,0)))
    newraw=box(b'tkhd',bytes(tkhd))+edts+box(b'tref',box(b'cdsc',integer(1)))+box(b'mdia',mdia)
    # Replace only the final movie box, so all encoded sample offsets stay put.
    moov=child(data,b'moov')
    changed=b''.join(box(b'trak',newraw) if k==b'trak' and p==raw else whole
                     for k,p,whole,_ in split_atoms(moov))
    path.write_bytes(b''.join(box(b'moov',changed) if k==b'moov' else whole
                             for k,p,whole,_ in split_atoms(data)))
    assert movie.inspect_live_photo_movie(path).supported


class Round2MovieTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source,self.output=self.root/'source.mov',self.root/'output.mov'

    def tearDown(self):
        self.tmp.cleanup()

    def run_rewrite(self,when=Fraction(1,2)):
        before=self.source.read_bytes()
        result=rewrite_original_marker(self.source,self.output,when)
        self.assertEqual(before,self.source.read_bytes())
        self.assertEqual(result['source_sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual(result['output_sha256'],hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertFalse(result['identifier_changed'])
        self.assertFalse(result['import_compatibility_verified'])
        self.assertEqual(movie.inspect_live_photo_movie(self.output).photo_time,when)
        return result

    def assert_native_shell(self,original,output):
        old=movie._load(original).tracks[2]
        new=next(t for t in movie._load(output).tracks if t.marker_ids)
        self.assertEqual(child(child(old.box.payload,b'mdia'),b'hdlr'),child(child(new.box.payload,b'mdia'),b'hdlr'))
        self.assertEqual(child(old.box.payload,b'tref'),child(new.box.payload,b'tref'))
        self.assertEqual(child(old.box.payload,b'tkhd')[:4],child(new.box.payload,b'tkhd')[:4])
        self.assertEqual(child(child(child(old.box.payload,b'mdia'),b'minf'),b'hdlr'),
                         child(child(child(new.box.payload,b'mdia'),b'minf'),b'hdlr'))
        self.assertEqual(child(child(child(old.box.payload,b'mdia'),b'minf'),b'dinf'),
                         child(child(child(new.box.payload,b'mdia'),b'minf'),b'dinf'))
        self.assertEqual([k.data for k in old.keys if k.kind in old.marker_ids],[k.data for k in new.keys])
        self.assertEqual(new.marker_ids,{integer(7)})
        self.assertEqual(new.metadata_samples,[box(integer(7),b'\xff')])
        self.assertEqual(new.scale,old.scale)
        self.assertEqual(child(new.stbl.payload,b'stts'),b'\0'*4+integer(1)+struct.pack('>II',1,1))

    def test_mixed_clone_preserves_native_shell_and_original_other_data(self):
        native_source(self.source)
        result=self.run_rewrite()
        self.assert_native_shell(self.source,self.output)
        old=movie._load(self.source);new=movie._load(self.output)
        self.assertEqual(result['marker_duration'],{'numerator':1,'denominator':1000})
        self.assertTrue(result['marker_duration_preserved'])
        self.assertTrue(result['mixed_track_split'])
        self.assertEqual(result['source_marker_track'],3)
        self.assertEqual(result['output_marker_track'],6)
        self.assertEqual(len(new.tracks),6)
        self.assertEqual(new.tracks[2].times,old.tracks[2].times)
        self.assertEqual(new.tracks[2].metadata_samples,[box(integer(2),b'second\0')])
        self.assertEqual([t.box.data for t in new.tracks[:2]+new.tracks[3:5]],
                         [t.box.data for t in old.tracks[:2]+old.tracks[3:5]])
        self.assertEqual(child(child(self.source.read_bytes(),b'moov'),b'meta'),
                         child(child(self.output.read_bytes(),b'moov'),b'meta'))
        self.assertTrue(result['trailing_moov_replaced_without_dead_index'])
        self.assertEqual([k for k,*_ in split_atoms(self.output.read_bytes())],[b'ftyp',b'mdat',b'mdat',b'moov'])

    def test_pure_marker_reuses_track_id_and_native_shell(self):
        native_source(self.source,mixed=False)
        result=self.run_rewrite()
        self.assert_native_shell(self.source,self.output)
        self.assertFalse(result['mixed_track_split'])
        self.assertEqual(result['source_marker_track'],result['output_marker_track'])
        self.assertEqual(result['track_count'],5)

    def test_zero_time_omits_zero_length_empty_edit(self):
        native_source(self.source)
        self.run_rewrite(Fraction(0))
        elst=child(child(marked_track(self.output)[0],b'edts'),b'elst')
        self.assertEqual(elst,b'\0'*4+integer(1)+struct.pack('>Iihh',1,0,1,0))

    def test_native_600_clock_keeps_one_600th_second_and_original_key_seven(self):
        native_source(self.source,clock=600)
        result=self.run_rewrite()
        self.assert_native_shell(self.source,self.output)
        self.assertEqual(result['marker_duration'],{'numerator':1,'denominator':600})
        self.assertEqual(result['marker_media_scale'],600)
        elst=child(child(marked_track(self.output)[0],b'edts'),b'elst')
        self.assertEqual(elst,b'\0'*4+integer(2)+struct.pack('>IihhIihh',300,-1,1,0,1,0,1,0))

    def test_version_one_native_clocks_and_edit_remain_version_one(self):
        native_source(self.source,clock=600)
        data=self.source.read_bytes();raw=marked_track(self.source)[0]
        tkhd=child(raw,b'tkhd');mdia=child(raw,b'mdia');mdhd=child(mdia,b'mdhd')
        newtkhd=full(b'tkhd',struct.pack('>QQIIQ',11,12,3,0,121)+tkhd[24:],version=1,flags=15)
        newmdhd=full(b'mdhd',struct.pack('>QQIQHH',11,12,600,1,21956,0),version=1)
        newedit=box(b'edts',full(b'elst',integer(2)+struct.pack('>QqhhQqhh',120,-1,1,0,1,0,1,0),version=1))
        updated=replace_child(raw,b'tkhd',newtkhd)
        updated=replace_child(updated,b'edts',newedit)
        updated=replace_child(updated,b'mdia',box(b'mdia',replace_child(mdia,b'mdhd',newmdhd)))
        moov=child(data,b'moov')
        self.source.write_bytes(b''.join(box(b'moov',b''.join(box(k,updated) if k==b'trak' and p==raw else whole
                 for k,p,whole,_ in split_atoms(moov))) if k==b'moov' else whole for k,p,whole,_ in split_atoms(data)))
        self.run_rewrite()
        result,mdia,_,_=marked_track(self.output)
        self.assertEqual(child(result,b'tkhd')[0],1)
        self.assertEqual(child(mdia,b'mdhd')[0],1)
        self.assertEqual(child(child(result,b'edts'),b'elst')[0],1)
        self.assertEqual(struct.unpack_from('>Q',child(result,b'tkhd'),28)[0],301)

    def test_new_track_id_is_reflected_in_movie_next_track_id(self):
        native_source(self.source)
        result=self.run_rewrite()
        next_id=struct.unpack('>I',child(child(self.output.read_bytes(),b'moov'),b'mvhd')[-4:])[0]
        self.assertEqual(next_id,result['output_marker_track']+1)

    def test_last_frame_keeps_original_duration_not_whole_frame(self):
        native_source(self.source)
        result=self.run_rewrite(Fraction(1,2))
        self.assertEqual(result['marker_duration'],{'numerator':1,'denominator':1000})
        self.assertNotEqual(result['marker_duration'],{'numerator':1,'denominator':2})

    def test_repeated_rewrites_reuse_existing_pure_clone(self):
        native_source(self.source)
        self.run_rewrite()
        output2=self.root/'again.mov'
        result=rewrite_original_marker(self.output,output2,Fraction(0))
        self.assertFalse(result['mixed_track_split'])
        self.assertEqual(result['output_marker_track'],6)
        self.assertEqual(result['track_count'],6)
        self.assertEqual(result['marker_duration'],{'numerator':1,'denominator':1000})
        self.assertEqual(movie.inspect_live_photo_movie(output2).marker_count,1)

    def test_front_moov_keeps_original_mdat_offsets(self):
        make_movie(self.source,mixed=True,dual_identifier='old-identifier',front_moov=True)
        result=self.run_rewrite()
        self.assertFalse(result['trailing_moov_replaced_without_dead_index'])
        self.assertTrue(result['verification']['all_original_mdat_payloads_preserved'])

    def test_auxv_track_bytes_are_untouched(self):
        native_source(self.source)
        data=self.source.read_bytes()
        self.source.write_bytes(data.replace(b'soun',b'auxv',1))
        original=tracks(self.source)[1]
        result=self.run_rewrite()
        self.assertEqual(tracks(self.output)[1],original)
        self.assertEqual(result['verification']['auxiliary_track_ids'],[2])

    def test_noninteger_movie_tick_refused_instead_of_rounding(self):
        make_movie(self.source,marker_only=True,dual_identifier='old-identifier',video_scale=30000,deltas=(1001,1001,27998))
        with self.assertRaisesRegex(ValueError,'不能由原 movie'):
            rewrite_original_marker(self.source,self.output,Fraction(1001,30000))
        self.assertFalse(self.output.exists())

    def test_missing_original_marker_refused(self):
        make_movie(self.source,dual_identifier='old-identifier')
        with self.assertRaisesRegex(ValueError,'唯一有效'):
            rewrite_original_marker(self.source,self.output,Fraction(1,2))
        self.assertFalse(self.output.exists())

    def test_missing_identifier_refused(self):
        make_movie(self.source,marker_only=True)
        with self.assertRaisesRegex(ValueError,'缺少配对标识'):
            rewrite_original_marker(self.source,self.output,Fraction(1,2))
        self.assertFalse(self.output.exists())

    def test_non_minus_one_marker_refused(self):
        native_source(self.source)
        old=movie._load(self.source).tracks[2]
        offset=old.samples[0][0]
        with self.source.open('r+b') as stream:
            stream.seek(offset+8);stream.write(b'\0')
        with self.assertRaisesRegex(ValueError,'int8 -1'):
            rewrite_original_marker(self.source,self.output,Fraction(1,2))
        self.assertFalse(self.output.exists())

    def test_unknown_sample_extension_refused(self):
        native_source(self.source)
        data=self.source.read_bytes();original=marked_track(self.source)[0]
        mdia=child(original,b'mdia');minf=child(mdia,b'minf');stbl=child(minf,b'stbl')
        changed=replace_child(original,b'mdia',box(b'mdia',replace_child(mdia,b'minf',
                    box(b'minf',replace_child(minf,b'stbl',box(b'stbl',stbl+box(b'zzzz',b'opaque')))))))
        moov=child(data,b'moov')
        self.source.write_bytes(b''.join(box(b'moov',b''.join(box(k,changed) if k==b'trak' and p==original else whole
                 for k,p,whole,_ in split_atoms(moov))) if k==b'moov' else whole for k,p,whole,_ in split_atoms(data)))
        with self.assertRaisesRegex(ValueError,'扩展'):
            rewrite_original_marker(self.source,self.output,Fraction(1,2))
        self.assertFalse(self.output.exists())

    def test_cancellation_preserves_source_and_no_destination(self):
        native_source(self.source)
        before=self.source.read_bytes();cancel=threading.Event();cancel.set()
        with self.assertRaises(InterruptedError):
            rewrite_original_marker(self.source,self.output,Fraction(1,2),cancel)
        self.assertFalse(self.output.exists());self.assertEqual(self.source.read_bytes(),before)

    def test_output_collision_and_same_source_refused(self):
        native_source(self.source)
        self.output.write_bytes(b'keep')
        for output in (self.output,self.source):
            with self.subTest(output=output),self.assertRaises(ValueError):
                rewrite_original_marker(self.source,output,Fraction(1,2))
        self.assertEqual(self.output.read_bytes(),b'keep')

    def test_postwrite_failure_cleans_only_new_file(self):
        native_source(self.source)
        before=self.source.read_bytes()
        with patch('livegallery.live_photo_native_movie._verify_preserved',side_effect=ValueError('controlled verification failure')):
            with self.assertRaisesRegex(ValueError,'controlled verification'):
                rewrite_original_marker(self.source,self.output,Fraction(1,2))
        self.assertFalse(self.output.exists());self.assertEqual(self.source.read_bytes(),before)


if __name__ == '__main__':
    unittest.main(verbosity=2)


