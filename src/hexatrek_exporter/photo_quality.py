"""Privacy-safe, bounded photo cache encoding with original-object fingerprints."""

from __future__ import annotations

import base64
import binascii
import hashlib
from io import BytesIO
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

PHOTO_QUALITIES = {"hi": (1000, 55), "med": (800, 50), "lo": (640, 45)}
_SOURCE_KEYS = ("source_md5", "source_sha256", "source_size_bytes")


def normalize_quality(value):
    """Return an explicit supported cache quality, rejecting ambiguous values."""
    if not isinstance(value, str) or value not in PHOTO_QUALITIES:
        raise ValueError("photo_quality must be 'hi', 'med' or 'lo'")
    return value


def _source_metadata(photo):
    if not any(key in photo for key in _SOURCE_KEYS):
        if "quality" in photo:
            raise ValueError("Compressed photo is missing original source fingerprints")
        return None
    md5, sha, size = (photo.get(key) for key in _SOURCE_KEYS)
    try:
        raw_md5 = base64.b64decode(md5, validate=True) if isinstance(md5, str) else b""
    except (ValueError, binascii.Error):
        raw_md5 = b""
    if (len(raw_md5) != 16 or base64.b64encode(raw_md5).decode("ascii") != md5
            or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None
            or type(size) is not int or size <= 0):
        raise ValueError("Invalid original photo source fingerprints")
    return dict(zip(_SOURCE_KEYS, (md5, sha, size)))


def can_reuse_photo(photo, quality) -> bool:
    """Whether verified local bytes can supply the requested quality without cloud data."""
    quality = normalize_quality(quality)
    source = _source_metadata(photo)
    previous = photo.get("quality")
    if previous is None:
        return True
    previous = normalize_quality(previous)
    return (PHOTO_QUALITIES[quality][0] <= PHOTO_QUALITIES[previous][0]
            or (source is not None and photo.get("sha256") == source["source_sha256"]))


def compress_photo(data, quality="med", *, existing=None):
    """Encode a sanitized JPEG, retaining fingerprints of the original cloud bytes.

    Already encoded bytes at the same quality are returned unchanged. Increasing
    quality requires original bytes, never upsampling an earlier lossy generation.
    """
    quality = normalize_quality(quality)
    digest = hashlib.sha256(data).hexdigest()
    source = _source_metadata(existing) if existing is not None else None
    if existing is not None:
        if "sha256" in existing and existing["sha256"] != digest:
            raise ValueError("Source photo digest does not match its bytes")
        if not can_reuse_photo({**existing, "sha256": digest}, quality):
            raise ValueError("Higher photo quality requires original bytes; download the original photo")
    if source is None:
        source = {
            "source_md5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii"),
            "source_sha256": digest,
            "source_size_bytes": len(data),
        }
    elif digest == source["source_sha256"]:
        actual_md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")
        if source["source_md5"] != actual_md5 or source["source_size_bytes"] != len(data):
            raise ValueError("Original photo fingerprints do not match its bytes")
    assert source is not None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if not (0 < width <= 20000 and 0 < height <= 20000 and width * height <= 40000000):
                    raise ValueError("Photo dimensions exceed safe decoding limits")
                if image.format not in ("JPEG", "PNG", "MPO"):
                    raise ValueError("Photo must be a complete supported JPEG or PNG")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
                limit, jpeg_quality = PHOTO_QUALITIES[quality]
                unchanged = (existing is not None and existing.get("quality") == quality
                             and image.format == "JPEG" and image.mode == "RGB"
                             and max(image.size) <= limit
                             and not any(key in image.info for key in ("exif", "icc_profile", "comment", "xmp")))
                if unchanged:
                    return data, {"quality": quality, "width": image.width, "height": image.height, **source}
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((limit, limit), Image.Resampling.LANCZOS)
                if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    rgb = Image.new("RGB", rgba.size, "white")
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb = oriented.convert("RGB")
                # A fresh image ensures no EXIF/GPS, XMP, ICC or comments survive.
                clean = Image.new("RGB", rgb.size)
                clean.paste(rgb)
                output = BytesIO()
                clean.save(output, format="JPEG", quality=jpeg_quality, optimize=True, progressive=True)
                return output.getvalue(), {"quality": quality, "width": clean.width, "height": clean.height, **source}
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError("Photo is corrupt or cannot be decoded safely") from None
