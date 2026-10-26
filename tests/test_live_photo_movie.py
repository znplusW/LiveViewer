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

from livegallery.live_photo_movie import (
    LivePhotoMovieError, inspect_live_photo_movie, media_payload_digests,
    rewrite_live_photo_movie, verify_movie_payload_preserved,
    video_presentation_times,
)


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
          legacy_mebx=False, reference_index=1):
    deltas = deltas or [1] * len(samples)
    duration = sum(deltas)
    tkhd = full(b"tkhd", struct.pack(">IIIII", 101, 102, track_id, 0, 1000)
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
                       edit=edit, co64=co64, extended=extended, dref_atom=dref_atom, reference_index=reference_index)
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


class LivePhotoMovieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.mov"
        self.output = self.root / "output.mov"

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_movie_adds_real_marker_at_exact_vfr_pts_and_preserves_audio_video(self):
        video, audio = make_movie(self.source)
        before = self.source.read_bytes()
        self.assertEqual(video_presentation_times(self.source), (Fraction(0), Fraction(1, 5), Fraction(1, 2)))
        result = rewrite_live_photo_movie(self.source, self.output, "not-required-to-be-a-uuid", Fraction(1, 2))
        self.assertTrue(result.supported)
        self.assertEqual(result.photo_time, Fraction(1, 2))
        self.assertEqual(result.asset_identifier, "not-required-to-be-a-uuid")
        self.assertEqual(result.marker_count, 1)
        self.assertEqual(result.track_count, 3)
        self.assertEqual(result.media_payload_digests, ((1, "vide", hashlib.sha256(b"".join(video)).hexdigest()),
                                                       (2, "soun", hashlib.sha256(b"".join(audio)).hexdigest())))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(verify_movie_payload_preserved(self.source, self.output), result.media_payload_digests)

    def test_fractional_pts_not_representable_in_original_movie_ticks_remains_exact(self):
        make_movie(self.source, video_scale=30000, deltas=(1001, 1001, 27998))
        result = rewrite_live_photo_movie(self.source, self.output, "fraction", Fraction(1001, 30000))
        self.assertEqual(result.photo_time, Fraction(1001, 30000))
        self.assertEqual(result.duration, Fraction(1))

    def test_repeated_cover_changes_have_one_marker_and_reuse_active_moov_slot(self):
        make_movie(self.source)
        rewrite_live_photo_movie(self.source, self.output, "first", Fraction(1, 5))
        previous = self.output
        def dead_indexes(data):
            return sum(kind == b"free" and payload[4:8] == b"mvhd"
                       for kind, payload, *_ in split_atoms(data))
        first_moov_count = dead_indexes(previous.read_bytes())
        for i, when in enumerate((Fraction(1, 2), Fraction(0), Fraction(1, 5), Fraction(1, 2))):
            destination = self.root / f"repeat-{i}.mov"
            result = rewrite_live_photo_movie(previous, destination, "later", when)
            self.assertEqual(result.marker_count, 1)
            self.assertEqual(result.photo_time, when)
            self.assertEqual(result.track_count, 3)
            atoms = split_atoms(destination.read_bytes())
            self.assertEqual(sum(k == b"moov" for k, *_ in atoms), 1)
            self.assertEqual(dead_indexes(destination.read_bytes()), first_moov_count)
            # A tiny metadata mdat is appended, not another dead movie/index.
            self.assertLessEqual(destination.stat().st_size - previous.stat().st_size, 25)
            previous = destination

    def test_existing_marker_only_track_replaced_without_extra_track(self):
        make_movie(self.source, marker_only=True)
        self.assertEqual(inspect_live_photo_movie(self.source).photo_time, Fraction(0))
        result = rewrite_live_photo_movie(self.source, self.output, "replacement", Fraction(1, 2))
        self.assertEqual(result.track_count, 3)
        self.assertEqual(result.marker_count, 1)
        self.assertEqual(result.photo_time, Fraction(1, 2))

    def test_mixed_marker_track_preserves_other_keys_values_and_sample_times(self):
        make_movie(self.source, mixed=True)
        self.assertEqual(inspect_live_photo_movie(self.source).photo_time, Fraction(1, 5))
        result = rewrite_live_photo_movie(self.source, self.output, "mixed", Fraction(1, 2))
        self.assertEqual(result.track_count, 4)
        self.assertEqual(result.marker_count, 1)
        moov = next(p for k, p, *_ in split_atoms(self.output.read_bytes()) if k == b"moov")
        tracks = [p for k, p, *_ in split_atoms(moov) if k == b"trak"]
        old_metadata = tracks[2]
        self.assertNotIn(KEY, old_metadata)
        self.assertIn(b"com.example.synthetic", old_metadata)
        # Decode the retained track's new chunk directly, independently of inspect.
        mdia = next(p for k, p, *_ in split_atoms(old_metadata) if k == b"mdia")
        minf = next(p for k, p, *_ in split_atoms(mdia) if k == b"minf")
        stbl = next(p for k, p, *_ in split_atoms(minf) if k == b"stbl")
        table = {k: p for k, p, *_ in split_atoms(stbl)}
        self.assertEqual(table[b"stts"], b"\0" * 4 + integer(2) + struct.pack(">IIII", 1, 2, 1, 1))
        chunk = struct.unpack_from(">Q", table[b"co64"], 8)[0]
        expected = box(integer(2), b"first\0") + box(integer(2), b"second\0")
        self.assertEqual(self.output.read_bytes()[chunk:chunk + len(expected)], expected)
        third = self.root / "third.mov"
        again = rewrite_live_photo_movie(self.output, third, "again", Fraction(1, 5))
        self.assertEqual(again.track_count, 4)
        self.assertEqual(again.marker_count, 1)

    def test_stco_co64_extended_headers_and_front_moov_keep_media_offsets(self):
        for i, options in enumerate(({}, {"co64": True}, {"extended": True, "co64": True},
                                     {"front_moov": True}, {"front_moov": True, "zero_mdat": True})):
            with self.subTest(options=options):
                source, destination = self.root / f"in{i}.mov", self.root / f"out{i}.mov"
                make_movie(source, **options)
                old_mdats = [(pos + (16 if raw[:4] == integer(1) else 8), p) for k, p, raw, pos in split_atoms(source.read_bytes()) if k == b"mdat"]
                rewrite_live_photo_movie(source, destination, "offset-test", Fraction(1, 5))
                for offset, payload in old_mdats:
                    self.assertEqual(destination.read_bytes()[offset:offset + len(payload)], payload)
                self.assertEqual(media_payload_digests(source), media_payload_digests(destination))

    def test_ctts_and_version0_or1_edit_lists_use_movie_presentation_timeline(self):
        for version in (0, 1):
            with self.subTest(version=version):
                source, output = self.root / f"edit{version}.mov", self.root / f"result{version}.mov"
                make_movie(source, ctts=(2, 2, 2), edit=(version, [(100, -1), (900, 2)]))
                self.assertEqual(video_presentation_times(source), (Fraction(1, 10), Fraction(3, 10), Fraction(3, 5)))
                result = rewrite_live_photo_movie(source, output, "edited", Fraction(3, 10))
                self.assertEqual(result.photo_time, Fraction(3, 10))
                self.assertEqual(media_payload_digests(source), result.media_payload_digests)

    def test_edit_list_excluded_frames_cannot_be_used_as_cover(self):
        make_movie(self.source, edit=(0, [(300, 0)]))
        self.assertEqual(video_presentation_times(self.source), (Fraction(0), Fraction(1, 5)))
        with self.assertRaisesRegex(LivePhotoMovieError, "实际呈现帧"):
            rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 2))
        self.assertFalse(self.output.exists())

    def test_duplicate_composition_pts_cannot_identify_a_unique_cover(self):
        make_movie(self.source, ctts=(0, -2, 0))
        self.assertEqual(video_presentation_times(self.source), (Fraction(0), Fraction(0), Fraction(1, 2)))
        with self.assertRaisesRegex(LivePhotoMovieError, "相同呈现时间"):
            rewrite_live_photo_movie(self.source, self.output, "id", Fraction(0))
        self.assertFalse(self.output.exists())
        result = rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 2))
        self.assertEqual(result.photo_time, Fraction(1, 2))

    def test_top_and_udta_identifiers_updated_without_changing_capture_date(self):
        make_movie(self.source, dual_identifier="old-identifier")
        result = rewrite_live_photo_movie(self.source, self.output, "new-id-测试", Fraction(1, 5))
        self.assertEqual(result.asset_identifier, "new-id-测试")
        moov = next(p for k, p, *_ in split_atoms(self.output.read_bytes()) if k == b"moov")
        self.assertEqual(moov.count("new-id-测试".encode()), 2)
        self.assertNotIn(b"old-identifier", moov)
        self.assertEqual(moov.count(b"2020-01-02T03:04:05+0800"), 2)

    def test_conflicting_identifiers_fail_closed_without_output(self):
        make_movie(self.source, dual_identifier="other-identifier")
        info = inspect_live_photo_movie(self.source)
        self.assertFalse(info.supported)
        self.assertIn("冲突", info.unsupported_reason)
        with self.assertRaises(LivePhotoMovieError):
            rewrite_live_photo_movie(self.source, self.output, "replacement", Fraction(1, 5))
        self.assertFalse(self.output.exists())

    def test_track_and_media_pairing_metadata_fail_closed_not_stale(self):
        for scope in ("track", "media", "track-user-data"):
            with self.subTest(scope=scope):
                make_movie(self.source)
                parts = split_atoms(self.source.read_bytes())
                moov = next(p for k, p, *_ in parts if k == b"moov")
                children = split_atoms(moov)
                original_track = next(raw for k, _, raw, _ in children if k == b"trak")
                track_parts = split_atoms(original_track[8:])
                tag = metadata("old-scoped-identifier")
                if scope == "media":
                    rewritten_track = box(b"trak", b"".join(box(k, p + tag) if k == b"mdia" else raw
                                                           for k, p, raw, _ in track_parts))
                else:
                    rewritten_track = box(b"trak", original_track[8:] + (box(b"udta", tag) if scope == "track-user-data" else tag))
                new_moov = box(b"moov", moov.replace(original_track, rewritten_track, 1))
                content = b"".join(new_moov if k == b"moov" else raw for k, _, raw, _ in parts)
                self.source.write_bytes(content)
                info = inspect_live_photo_movie(self.source)
                self.assertFalse(info.supported)
                self.assertIn("ContentIdentifier", info.unsupported_reason)
                with self.assertRaises(LivePhotoMovieError):
                    rewrite_live_photo_movie(self.source, self.output, "new-id", Fraction(1, 5))
                self.assertFalse(self.output.exists())
                self.assertEqual(self.source.read_bytes(), content)

    def test_timed_content_identifier_cannot_survive_as_a_conflicting_pairing(self):
        make_movie(self.source, mixed=True, extra_metadata_key=ID_KEY)
        info = inspect_live_photo_movie(self.source)
        self.assertFalse(info.supported)
        self.assertIn("时序ContentIdentifier", info.unsupported_reason)
        with self.assertRaises(LivePhotoMovieError):
            rewrite_live_photo_movie(self.source, self.output, "new-id", Fraction(1, 5))
        self.assertFalse(self.output.exists())

    def test_invalid_or_estimated_time_and_existing_destination_never_change_inputs(self):
        make_movie(self.source)
        original = self.source.read_bytes()
        for value in (0.2, Fraction(-1), Fraction(1), Fraction(1, 7)):
            with self.subTest(value=value), self.assertRaises(LivePhotoMovieError):
                rewrite_live_photo_movie(self.source, self.output, "id", value)
            self.assertFalse(self.output.exists())
        self.output.write_bytes(b"must not replace")
        with self.assertRaises(LivePhotoMovieError):
            rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
        self.assertEqual(self.output.read_bytes(), b"must not replace")
        self.assertEqual(self.source.read_bytes(), original)

    def test_fragmented_truncated_and_external_media_are_explained_and_rejected(self):
        make_movie(self.source)
        data = self.source.read_bytes()
        variants = (data + box(b"moof"), data[:-1], data.replace(full(b"url ", flags=1), full(b"url ", flags=0)))
        for i, content in enumerate(variants):
            path = self.root / f"bad{i}.mov"
            path.write_bytes(content)
            info = inspect_live_photo_movie(path)
            self.assertFalse(info.supported)
            self.assertTrue(info.unsupported_reason)
            with self.assertRaises(LivePhotoMovieError):
                rewrite_live_photo_movie(path, self.output, "id", Fraction(1, 5))
            self.assertFalse(self.output.exists())

    def test_self_contained_alias_references_preserve_all_original_media(self):
        for index, options in enumerate(({}, {"co64": True, "extended": True})):
            with self.subTest(options=options):
                source, target = self.root / f"alias-{index}.mov", self.root / f"alias-out-{index}.mov"
                make_movie(source, mixed=True, dref_atom=full(b"dref", integer(1) + full(b"alis", flags=1)), **options)
                original = source.read_bytes()
                info = inspect_live_photo_movie(source)
                self.assertTrue(info.supported, info.unsupported_reason)
                self.assertEqual(info.photo_time, Fraction(1, 5))
                result = rewrite_live_photo_movie(source, target, "alias-id", Fraction(1, 2))
                self.assertEqual(result.photo_time, Fraction(1, 2))
                self.assertEqual(result.marker_count, 1)
                self.assertEqual(verify_movie_payload_preserved(source, target), result.media_payload_digests)
                self.assertEqual(source.read_bytes(), original)

    def test_alias_support_does_not_allow_external_ambiguous_or_malformed_references(self):
        child_cases = [full(kind, flags=flags) for kind in (b"url ", b"alis") for flags in (0, 2, 3)]
        child_cases += [full(b"alis", version=1, flags=1), full(b"url ", version=1, flags=1),
                        full(b"alis", b"external-path", flags=1), full(b"url ", b"file:///external\0", flags=1),
                        full(b"urn ", flags=1), box(b"alis", b"\0\0\1")]
        refs = [full(b"dref", integer(1) + child) for child in child_cases]
        good = full(b"alis", flags=1)
        refs += [full(b"dref", integer(1) + good, version=1), full(b"dref", integer(1) + good, flags=1),
                 full(b"dref", integer(0)), full(b"dref", integer(2) + good),
                 full(b"dref", integer(2) + good + good)]
        for index, atom in enumerate(refs):
            with self.subTest(index=index):
                make_movie(self.source, dref_atom=atom)
                before = self.source.read_bytes()
                self.assertFalse(inspect_live_photo_movie(self.source).supported)
                with self.assertRaises(LivePhotoMovieError):
                    rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
                self.assertFalse(self.output.exists())
                self.assertEqual(self.source.read_bytes(), before)

    def test_alias_reference_still_requires_index_one_and_local_mdat_ranges(self):
        reference = full(b"dref", integer(1) + full(b"alis", flags=1))
        for index in (0, 2):
            with self.subTest(index=index):
                make_movie(self.source, dref_atom=reference, reference_index=index)
                self.assertFalse(inspect_live_photo_movie(self.source).supported)
                with self.assertRaises(LivePhotoMovieError):
                    rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
                self.assertFalse(self.output.exists())
        make_movie(self.source, dref_atom=reference)
        raw = self.source.read_bytes()
        first_offset = raw.index(b"stco") + 12
        raw = raw[:first_offset] + integer(len(raw) + 100) + raw[first_offset + 4:]
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(LivePhotoMovieError, "mdat"):
            rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
        self.assertFalse(self.output.exists())

    def test_standard_and_exact_legacy_mebx_inputs_emit_canonical_header(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                source, target = self.root / f"mebx-{legacy}.mov", self.root / f"canonical-{legacy}.mov"
                make_movie(source, mixed=True, legacy_mebx=legacy)
                self.assertTrue(inspect_live_photo_movie(source).supported)
                result = rewrite_live_photo_movie(source, target, "canonical", Fraction(1, 2))
                self.assertEqual(result.marker_count, 1)
                moov = next(p for k, p, *_ in split_atoms(target.read_bytes()) if k == b"moov")
                entries = []
                for k, trk, *_ in split_atoms(moov):
                    if k != b"trak": continue
                    mdia = next(p for k, p, *_ in split_atoms(trk) if k == b"mdia")
                    handler = next(p for k, p, *_ in split_atoms(mdia) if k == b"hdlr")
                    if handler[8:12] != b"meta": continue
                    minf = next(p for k, p, *_ in split_atoms(mdia) if k == b"minf")
                    stbl = next(p for k, p, *_ in split_atoms(minf) if k == b"stbl")
                    stsd = next(p for k, p, *_ in split_atoms(stbl) if k == b"stsd")
                    entries += split_atoms(stsd, 8)
                self.assertEqual(len(entries), 2)
                for kind, payload, *_ in entries:
                    self.assertEqual(kind, b"mebx")
                    self.assertEqual(payload[:8], b"\0" * 6 + b"\0\1")
                    self.assertEqual(split_atoms(payload, 8)[0][0], b"keys")
                self.assertEqual(verify_movie_payload_preserved(source, target), result.media_payload_digests)

    def test_legacy_mebx_compatibility_never_scans_past_damaged_padding_or_keys(self):
        make_movie(self.source, marker_only=True, legacy_mebx=True)
        original = self.source.read_bytes()
        entry = original.index(b"mebx") - 4
        keys = entry + 24
        cases = [(entry + 16, b"\0\0\0\1"), (entry + 16, b"badpad!!"),
                 (keys + 4, b"free"), (keys, integer(len(original) + 1)),
                 (original.index(b"keyd"), b"bad!"), (original.index(b"dtyp"), b"bad!")]
        for index, (offset, replacement) in enumerate(cases):
            with self.subTest(index=index):
                content = original[:offset] + replacement + original[offset + len(replacement):]
                self.source.write_bytes(content)
                self.assertFalse(inspect_live_photo_movie(self.source).supported)
                with self.assertRaises(LivePhotoMovieError):
                    rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
                self.assertFalse(self.output.exists())
                self.assertEqual(self.source.read_bytes(), content)

    def test_five_track_alias_movie_preserves_auxiliary_metadata_tracks_verbatim(self):
        make_movie(self.source, mixed=True, other_metadata_tracks=True,
                   dref_atom=full(b"dref", integer(1) + full(b"alis", flags=1)))
        self.source.write_bytes(self.source.read_bytes().replace(b"mp4a", b"lpcm"))
        original = self.source.read_bytes()
        self.assertEqual(inspect_live_photo_movie(self.source).track_count, 5)
        result = rewrite_live_photo_movie(self.source, self.output, "five-track", Fraction(1, 2))
        self.assertEqual(result.track_count, 6)
        self.assertEqual(result.marker_count, 1)
        def tracks(data):
            moov = next(p for k, p, *_ in split_atoms(data) if k == b"moov")
            return [raw for k, _, raw, _ in split_atoms(moov) if k == b"trak"]
        before_tracks, after_tracks = tracks(original), tracks(self.output.read_bytes())
        for index in (0, 1, 3, 4):
            self.assertEqual(before_tracks[index], after_tracks[index])
        for _, payload, raw, offset in (a for a in split_atoms(original) if a[0] == b"mdat"):
            start = offset + len(raw) - len(payload)
            self.assertEqual(self.output.read_bytes()[start:start + len(payload)], payload)
        self.assertEqual(verify_movie_payload_preserved(self.source, self.output), result.media_payload_digests)
        self.assertEqual(self.source.read_bytes(), original)

    def test_old_test_build_auxiliary_metadata_is_canonicalized_without_sample_changes(self):
        make_movie(self.source, marker_only=True, other_metadata_tracks=True, legacy_mebx=True)
        original = self.source.read_bytes()
        result = rewrite_live_photo_movie(self.source, self.output, "legacy-whole-movie", Fraction(1, 2))
        self.assertEqual(result.track_count, 5)
        self.assertEqual(result.marker_count, 1)
        def metadata_parts(data):
            moov = next(p for k, p, *_ in split_atoms(data) if k == b"moov")
            output = []
            for k, trk, *_ in split_atoms(moov):
                if k != b"trak": continue
                track_parts = {k: (p, raw) for k, p, raw, _ in split_atoms(trk)}
                media_parts = {k: (p, raw) for k, p, raw, _ in split_atoms(track_parts[b"mdia"][0])}
                if media_parts[b"hdlr"][0][8:12] != b"meta": continue
                minf = {k: (p, raw) for k, p, raw, _ in split_atoms(media_parts[b"minf"][0])}
                stbl = {k: (p, raw) for k, p, raw, _ in split_atoms(minf[b"stbl"][0])}
                entry = split_atoms(stbl[b"stsd"][0], 8)[0][1]
                output.append((entry, track_parts[b"tkhd"][1], media_parts[b"mdhd"][1],
                               {k: raw for k, (_, raw) in stbl.items() if k != b"stsd"}))
            return output
        before, after = metadata_parts(original), metadata_parts(self.output.read_bytes())
        self.assertEqual(len(before), 3)
        for old, new in zip(before[1:], after[1:]):
            self.assertEqual(old[0][8:16], b"\0" * 8)
            self.assertEqual(new[0], old[0][:8] + old[0][16:])
            self.assertEqual(old[1:], new[1:])
        self.assertEqual(verify_movie_payload_preserved(self.source, self.output), result.media_payload_digests)
        for _, payload, raw, offset in (a for a in split_atoms(original) if a[0] == b"mdat"):
            start = offset + len(raw) - len(payload)
            self.assertEqual(self.output.read_bytes()[start:start + len(payload)], payload)

    def test_cancellation_before_or_after_write_removes_only_own_stage(self):
        make_movie(self.source)
        original = self.source.read_bytes()
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5), cancel_event=event)
        self.assertFalse(self.output.exists())
        with patch("livegallery.live_photo_movie.verify_movie_payload_preserved", side_effect=InterruptedError("cancel")):
            with self.assertRaises(InterruptedError):
                rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_payload_corruption_is_detected_without_touching_source(self):
        make_movie(self.source)
        rewrite_live_photo_movie(self.source, self.output, "id", Fraction(1, 5))
        content = self.output.read_bytes().replace(b"video-sample-0", b"Video-sample-0", 1)
        self.output.write_bytes(content)
        with self.assertRaisesRegex(LivePhotoMovieError, "编码样本"):
            verify_movie_payload_preserved(self.source, self.output)

    def test_repository_synthetic_h264_hevc_and_vfr_movies_preserve_encoded_samples(self):
        fixtures = Path(__file__).parent / "fixtures"
        for name in ("Live_H264.MOV", "Live_HEVC.MOV", "VFR_PAIR.mov", "Portrait_HEVC.mov"):
            source = next(p for p in fixtures.iterdir() if p.name.casefold() == name.casefold())
            with self.subTest(name=name):
                times = video_presentation_times(source)
                target_time = times[len(times) // 2]
                result = rewrite_live_photo_movie(source, self.root / name, "synthetic-" + name, target_time)
                self.assertEqual(result.photo_time, target_time)
                self.assertEqual(result.media_payload_digests, media_payload_digests(source))


if __name__ == "__main__":
    unittest.main()
