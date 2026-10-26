"""Create Live Photo metadata on explicitly synthetic test resources only.

No personal assets belong in these fixtures. Movie media packets and their
timing are copied without decoding; image pixels are unchanged by metadata.
"""
from fractions import Fraction
from pathlib import Path
import struct
import tempfile

from PIL import Image

from livegallery import live_photo_movie as movie
from livegallery import live_photo_native_image as metadata
from livegallery.heic_encoder import encode_heic

DEFAULT_IDENTIFIER = '11111111-2222-3333-4444-555555555555'


def apple_exif(identifier=DEFAULT_IDENTIFIER):
    encoded = identifier.encode('utf8') + b'\0'
    fields = [(1, 9, 1, struct.pack('>i', 14)),
              (17, 2, len(encoded), encoded),
              (23, 16, 1, struct.pack('>Q', 8595185700)),
              (65433, 7, 17, b'synthetic-opaque\0')]
    prefix_size = 16 + len(fields) * 12 + 4
    entries, payload = [], bytearray()
    for tag, kind, count, data in fields:
        if len(data) <= 4:
            value = data.ljust(4, b'\0')
        else:
            if (prefix_size + len(payload)) % 2:
                payload += b'\0'
            value = struct.pack('>I', prefix_size + len(payload))
            payload += data
        entries.append(struct.pack('>HHI', tag, kind, count) + value)
    note = b'Apple iOS\0\0\1MM' + struct.pack('>H', len(fields)) + b''.join(entries) + b'\0' * 4 + payload
    value = Image.Exif()
    value[0x10f], value[0x110], value[0x112] = 'Apple', 'Synthetic Live Photo', 1
    value[0x8769] = {0x927c: note, 0x9003: '2025:02:03 04:05:06', 0x9011: '+08:00'}
    return value.tobytes()


def prepare_live_photo_pair(image_path, movie_path, *, identifier=DEFAULT_IDENTIFIER,
                            photo_time=Fraction(0)):
    """Add Apple/marker metadata to an already generated synthetic pair in place."""
    image_path, movie_path = Path(image_path), Path(movie_path)
    with tempfile.TemporaryDirectory(prefix='live-photo-fixture-', dir=image_path.parent) as temporary:
        temporary = Path(temporary)
        exif = temporary / 'capture.exif'
        exif.write_bytes(apple_exif(identifier)[6:])  # ExifTool EXIF import expects the TIFF header.
        image_stage = temporary / ('image' + image_path.suffix)
        metadata._run(['-EXIF<=' + str(exif), '-o', image_stage, image_path])
        movie_stage = temporary / 'movie.MOV'
        movie.rewrite_live_photo_movie(movie_path, movie_stage, identifier, photo_time)
        loaded = movie._load(movie_stage)
        marker = next(track for track in loaded.tracks if track.marker_ids)
        video = next(track for track in loaded.tracks if track.handler == b'vide')
        children = movie._children(marker.box)
        mdia = movie._one(children, b'mdia')
        name = b'Core Media Metadata'
        handler = movie._full(b'hdlr', b'mhlrmetaappl' + b'\0' * 8 + bytes([len(name)]) + name)
        tkhd = movie._one(children, b'tkhd')
        track = movie._replace(marker.box, {
            b'tkhd': movie._atom(b'tkhd', tkhd.payload[:1] + b'\0\0\x0f' + tkhd.payload[4:]),
            b'mdia': movie._replace(mdia, {b'hdlr': handler}),
        })
        if movie._one(children, b'tref', optional=True) is None:
            track = movie._atom(b'trak', track[8:] + movie._atom(b'tref', movie._atom(b'cdsc', movie._pack(video.identifier))))
        rewritten = movie._atom(b'moov', b''.join(track if box is marker.box else box.data for box in loaded.children))
        # Retire a non-tail index in place, retaining every media sample offset.
        with movie_stage.open('r+b') as stream:
            if loaded.top[-1].kind == b'moov':
                stream.seek(loaded.moov.start)
            else:
                stream.seek(loaded.moov.start + 4)
                stream.write(b'free')
                stream.seek(0, 2)
            stream.write(rewritten)
            stream.truncate()
        checked = movie.inspect_live_photo_movie(movie_stage)
        assert checked.asset_identifier == identifier and checked.photo_time == photo_time and checked.marker_count == 1
        image_stage.replace(image_path)
        movie_stage.replace(movie_path)


def create_live_photo_pair(image_path, movie_path, *, video_fixture=None, pixels=None,
                           identifier=DEFAULT_IDENTIFIER, photo_time=Fraction(0)):
    """Create a synthetic image plus a copied generated movie, then annotate."""
    image_path, movie_path = Path(image_path), Path(movie_path)
    if image_path.exists() or movie_path.exists():
        raise FileExistsError('Synthetic fixture destinations must be new')
    pixels = pixels if pixels is not None else Image.new('RGB', (80, 60), 'blue')
    if image_path.suffix.casefold() == '.heic':
        encode_heic(pixels, image_path)
    else:
        pixels.save(image_path, 'JPEG')
    fixture = Path(video_fixture) if video_fixture else Path(__file__).parent / 'fixtures/VFR_PAIR.MOV'
    movie_path.write_bytes(fixture.read_bytes())
    prepare_live_photo_pair(image_path, movie_path, identifier=identifier, photo_time=photo_time)
