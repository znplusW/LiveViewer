"""Fresh cover EXIF with a bounded Apple MakerNote ContentIdentifier.

MakerNote tag 0x0011 is Apple's ContentIdentifier, not EXIF ImageUniqueID.
The Apple IFD starts 14 bytes into its note; offsets are note-relative:
https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/Apple.pm
https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/MakerNotes.pm

Only capture provenance is copied. Old thumbnails, orientation, dimensions,
exposure/rendering fields and opaque MakerNotes describe the old pixels and
must not survive a replacement video frame. Missing time zones stay missing.
"""
from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re
import struct

from PIL import Image

from . import imaging as _imaging  # Register the existing HEIC reader.

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
MAKER_NOTE = 0x927C
CONTENT_IDENTIFIER = 0x0011
MAX_IDENTIFIER_BYTES = 512
MAX_EXIF_BYTES = 60 * 1024  # Fits a single JPEG APP1 segment, including header.
_APPLE_PREFIX = b'Apple iOS\x00\x00\x01'
_SAFE_TEXT = (0x010E, 0x010F, 0x0110, 0x013B, 0x8298)  # description/make/model/artist/copyright


def _identifier_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError('Live Photo 关联标识不能为空。')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Live Photo 关联标识含无效控制字符。')
    try:
        encoded = value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('Live Photo 关联标识不是有效 UTF-8。') from error
    if len(encoded) > MAX_IDENTIFIER_BYTES:
        raise ValueError('Live Photo 关联标识过长。')
    return encoded


def _maker_note(identifier: str) -> bytes:
    value = _identifier_bytes(identifier) + b'\x00'
    # 14-byte header, IFD count, one 12-byte ASCII entry, next-IFD offset.
    offset = 14 + 2 + 12 + 4
    stored = value.ljust(4, b'\x00') if len(value) <= 4 else struct.pack('>I', offset)
    return (_APPLE_PREFIX + b'MM' + struct.pack('>H', 1)
            + struct.pack('>HHI', CONTENT_IDENTIFIER, 2, len(value)) + stored
            + struct.pack('>I', 0) + (value if len(value) > 4 else b''))


def _read_maker_identifier(note) -> str | None:
    if not isinstance(note, bytes) or not note.startswith(b'Apple iOS\x00'):
        return None
    if len(note) > MAX_EXIF_BYTES or len(note) < 20 or note[12:14] not in (b'MM', b'II'):
        raise ValueError('Apple MakerNote 结构无效。')
    endian = '>' if note[12:14] == b'MM' else '<'
    count = struct.unpack_from(endian + 'H', note, 14)[0]
    end = 16 + count * 12
    if count > 1024 or end + 4 > len(note):
        raise ValueError('Apple MakerNote 目录越界。')
    values = []
    for position in range(16, end, 12):
        tag, kind, length, pointer = struct.unpack_from(endian + 'HHII', note, position)
        if tag != CONTENT_IDENTIFIER:
            continue
        if kind != 2 or not 1 < length <= MAX_IDENTIFIER_BYTES + 1:
            raise ValueError('Apple 关联标识类型或长度无效。')
        start = position + 8 if length <= 4 else pointer
        if (length > 4 and start < end + 4) or start + length > len(note):
            raise ValueError('Apple 关联标识数据越界。')
        raw = note[start:start + length]
        if not raw.endswith(b'\x00'):
            raise ValueError('Apple 关联标识未终止。')
        try:
            value = raw[:-1].decode('utf-8')
        except UnicodeError as error:
            raise ValueError('Apple 关联标识编码无效。') from error
        _identifier_bytes(value)
        values.append(value)
    if len(values) > 1:
        raise ValueError('Apple MakerNote 包含重复关联标识。')
    return values[0] if values else None


def _metadata(path: Path):
    path = Path(path)
    if path.suffix.casefold() not in ('.jpg', '.jpeg', '.heic'):
        raise ValueError('Live Photo 封面仅支持 JPEG 或 HEIC。')
    with Image.open(path) as image:
        expected = 'HEIF' if path.suffix.casefold() == '.heic' else 'JPEG'
        if image.format != expected:
            raise ValueError('封面扩展名与实际格式不匹配。')
        raw = image.info.get('exif', b'')
        if expected == 'HEIF':
            # The Pillow HEIF opener normalizes EXIF Orientation in memory.
            # Validate the stored bytes, not that normalized copy.
            from pi_heif import open_heif
            raw = open_heif(path).info.get('exif') or b''
        if isinstance(raw, bytes) and len(raw) > 1024 * 1024:
            raise ValueError('源图片 EXIF 超出安全读取范围。')
        exif = Image.Exif()
        if raw:
            exif.load(raw)
        # Resolve nested offsets before closing the image's stream.
        details = dict(exif.get_ifd(EXIF_IFD)) if EXIF_IFD in exif else {}
        gps = dict(exif.get_ifd(GPS_IFD)) if GPS_IFD in exif else {}
        return dict(exif), details, gps, image.size


def _text(value, limit=1024):
    if isinstance(value, str) and len(value) <= limit and '\x00' not in value:
        return value
    return None


def _date(value, format):
    value = _text(value, 32)
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, format)
    except ValueError:
        return None
    return value if parsed.strftime(format) == value else None


def _rational(value, minimum=0.0, maximum=math.inf):
    if isinstance(value, (str, bytes, bool)):
        return None
    try:
        number = float(value)
        if math.isfinite(number) and minimum <= number <= maximum:
            return value
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        pass
    return None


def _triple(value, maxima):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    if all(_rational(number, 0, maximum) is not None for number, maximum in zip(value, maxima)):
        return tuple(value)
    return None


def _capture_gps(gps):
    result = {}
    for ref_tag, value_tag, refs, bound in ((1, 2, ('N', 'S'), 90), (3, 4, ('E', 'W'), 180)):
        value = _triple(gps.get(value_tag), (bound, 59.999999999, 59.999999999))
        if value is not None and gps.get(ref_tag) in refs:
            degrees, minutes, seconds = map(float, value)
            if degrees + minutes / 60 + seconds / 3600 <= bound:
                result[ref_tag], result[value_tag] = gps[ref_tag], value
    altitude = _rational(gps.get(6))
    ref = gps.get(5)
    if isinstance(ref, bytes) and len(ref) == 1:
        ref = ref[0]
    if altitude is not None and ref in (0, 1):
        result[5], result[6] = ref, altitude
    timestamp = _triple(gps.get(7), (23, 59, 59.999999999))
    if timestamp is not None:
        result[7] = timestamp
    date = _date(gps.get(29), '%Y:%m:%d')
    if date is not None:
        result[29] = date
    datum = _text(gps.get(18), 128)
    if datum:
        result[18] = datum
    error = _rational(gps.get(31))
    if error is not None:
        result[31] = error
    version = gps.get(0)
    if result and isinstance(version, (bytes, tuple, list)) and len(version) == 4:
        if all(type(value) is int and 0 <= value <= 255 for value in version):
            result[0] = bytes(version)
    return result


def build_cover_exif(source: Path, asset_identifier: str, size: tuple[int, int]) -> bytes:
    """Return new EXIF bytes; never modify the source or invent a capture zone."""
    _identifier_bytes(asset_identifier)
    if (not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(value) is not int or not 1 <= value <= 100_000 for value in size)
            or size[0] * size[1] > 100_000_000):
        raise ValueError('封面图片尺寸无效。')
    primary, original, gps, _ = _metadata(source)
    output = Image.Exif()
    for tag in _SAFE_TEXT:
        value = _text(primary.get(tag))
        if value is not None:
            output[tag] = value
    modified = datetime.now().astimezone()
    output[0x0132] = modified.strftime('%Y:%m:%d %H:%M:%S')
    output[0x0100], output[0x0101] = size
    output[0x0112] = 1
    output[0x0131] = 'LiveViewer'
    zone = modified.strftime('%z')
    details = {0xA002: size[0], 0xA003: size[1], MAKER_NOTE: _maker_note(asset_identifier),
               0x9010: zone[:3] + ':' + zone[3:], 0x9290: f'{modified.microsecond:06d}'}
    for tag in (0x9003, 0x9004):
        date = _date(original.get(tag), '%Y:%m:%d %H:%M:%S')
        if date is not None:
            details[tag] = date
    for tag in (0x9011, 0x9012):
        value = original.get(tag)
        if isinstance(value, str) and re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d', value):
            if value[1:3] != '14' or value[-2:] == '00':
                details[tag] = value
    for tag in (0x9291, 0x9292):
        value = original.get(tag)
        if isinstance(value, str) and re.fullmatch(r'\d{1,9}', value):
            details[tag] = value
    version = original.get(0x9000)
    if isinstance(version, bytes) and re.fullmatch(rb'\d{4}', version):
        details[0x9000] = version
    output[EXIF_IFD] = details
    location = _capture_gps(gps)
    if location:
        output[GPS_IFD] = location
    encoded = output.tobytes()
    if len(encoded) > MAX_EXIF_BYTES:
        raise ValueError('封面 EXIF 超过 JPEG/HEIC 兼容长度。')
    return encoded


def read_content_identifier(path: Path) -> str | None:
    """Read only Apple tag 17; absent or unusable notes have no usable ID."""
    _, details, _, _ = _metadata(path)
    try:
        return _read_maker_identifier(details.get(MAKER_NOTE))
    except ValueError:
        return None


def validate_cover_metadata(path: Path, identifier: str) -> None:
    """Check persisted EXIF and actual dimensions before the caller commits."""
    _identifier_bytes(identifier)
    primary, details, _, size = _metadata(path)
    if _read_maker_identifier(details.get(MAKER_NOTE)) != identifier:
        raise ValueError('保存后的 Apple Live Photo 关联标识不匹配。')
    if primary.get(0x0112) != 1:
        raise ValueError('保存后的封面方向不正确。')
    if (primary.get(0x0100), primary.get(0x0101)) != size:
        raise ValueError('保存后的封面尺寸与 EXIF 不匹配。')
    if (details.get(0xA002), details.get(0xA003)) != size:
        raise ValueError('保存后的 EXIF 像素尺寸不匹配。')
