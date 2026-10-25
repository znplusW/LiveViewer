"""Read container payloads independently of the Live Photo metadata writer."""
import struct


def boxes(data):
    offset = 0
    while offset < len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            size, header = struct.unpack_from('>Q', data, offset + 8)[0], 16
        if size == 0:
            size = len(data) - offset
        assert size >= header and offset + size <= len(data)
        yield kind, data[offset + header:offset + size]
        offset += size


def assert_video_media_preserved(test, original, updated):
    def read(data):
        top = list(boxes(data))
        moov = next(payload for kind, payload in top if kind == b'moov')
        tracks = []
        for kind, track in boxes(moov):
            if kind != b'trak':
                continue
            mdia = next(payload for kind, payload in boxes(track) if kind == b'mdia')
            handler = next(payload for kind, payload in boxes(mdia) if kind == b'hdlr')
            if handler[8:12] in (b'vide', b'soun'):
                tracks.append(track)
        return tracks, [payload for kind, payload in top if kind == b'mdat']
    original_tracks, original_data = read(original)
    updated_tracks, updated_data = read(updated)
    test.assertTrue(original_tracks)
    test.assertEqual(updated_tracks, original_tracks)
    test.assertEqual(updated_data[:len(original_data)], original_data)
    test.assertNotEqual(original, updated)
