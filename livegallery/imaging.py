"""Image decoding independent of the UI, including portable HEIC support."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

try:
    from pi_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def load_image(path: Path, max_edge: int | None = None) -> Image.Image:
    with Image.open(path) as source:
        source.seek(0)
        original_size = source.size
        profile = source.info.get("icc_profile")
        if max_edge and source.format == "JPEG":
            source.draft("RGB", (max_edge, max_edge))
        result = ImageOps.exif_transpose(source)
        if max_edge:
            result.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if profile:
            try:
                result = ImageCms.profileToProfile(
                    result, ImageCms.ImageCmsProfile(BytesIO(profile)),
                    ImageCms.createProfile("sRGB"), outputMode="RGBA" if "A" in result.getbands() else "RGB")
            except (ValueError, OSError, TypeError, ImageCms.PyCMSError):
                pass
        if "A" in result.getbands() or result.mode == "P" and "transparency" in result.info:
            rgba = result.convert("RGBA")
            background = Image.new("RGB", result.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            result = background
        else:
            result = result.convert("RGB")
        result = result.copy()
        result.info["original_size"] = original_size
        return result


def thumbnail_bytes(path: Path, edge: int = 256) -> bytes:
    if edge < 1:
        raise ValueError("Thumbnail edge must be positive")
    result = load_image(path, edge)
    stream = BytesIO()
    result.save(stream, format="JPEG", quality=84, optimize=False)
    return stream.getvalue()
