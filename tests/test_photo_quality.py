"""Synthetic-only image encoding and original-object identity tests."""

import base64
import hashlib
from io import BytesIO
import struct
import zlib

from PIL import Image
import pytest

from hexatrek_exporter.photo_quality import PHOTO_QUALITIES, can_reuse_photo, compress_photo, normalize_quality


def image_bytes(size=(1600, 1000), mode="RGB", color: str | tuple = "red", **save):
    image = Image.new(mode, size, color)
    stream = BytesIO()
    image.save(stream, format="PNG" if mode == "RGBA" else "JPEG", **save)
    return stream.getvalue()


def entry(data, metadata):
    return {**metadata, "sha256": hashlib.sha256(data).hexdigest()}


def test_exact_presets_and_default():
    assert PHOTO_QUALITIES == {"hi": (1000, 55), "med": (800, 50), "lo": (640, 45)}
    original = image_bytes()
    data, metadata = compress_photo(original)
    assert (metadata["width"], metadata["height"], metadata["quality"]) == (800, 500, "med")
    assert metadata["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert metadata["source_md5"] == base64.b64encode(hashlib.md5(original, usedforsecurity=False).digest()).decode()
    assert metadata["source_size_bytes"] == len(original)
    with Image.open(BytesIO(data)) as image:
        assert image.mode == "RGB" and image.format == "JPEG"
        assert image.info["progressive"] == 1


@pytest.mark.parametrize("value", [None, True, 1, [], "medium", "MED", " med "])
def test_invalid_quality(value):
    with pytest.raises(ValueError, match="photo_quality"):
        normalize_quality(value)


def test_repeated_encoding_is_byte_stable_and_downgrade_keeps_original_identity():
    original = image_bytes()
    high, fingerprints = compress_photo(original, "hi")
    repeated, repeated_metadata = compress_photo(high, "hi", existing=entry(high, fingerprints))
    assert repeated == high and repeated_metadata == fingerprints
    low, low_metadata = compress_photo(high, "lo", existing=entry(high, fingerprints))
    assert low_metadata["width"] == 640
    for key in ("source_md5", "source_sha256", "source_size_bytes"):
        assert low_metadata[key] == fingerprints[key]
    assert not can_reuse_photo(entry(low, low_metadata), "med")
    with pytest.raises(ValueError, match="requires original"):
        compress_photo(low, "med", existing=entry(low, low_metadata))


def test_original_bytes_can_supply_higher_quality():
    original = image_bytes()
    _, metadata = compress_photo(original, "lo")
    assert can_reuse_photo(entry(original, metadata), "hi")
    high, result = compress_photo(original, "hi", existing=entry(original, metadata))
    assert result["width"] == 1000 and high != original
    assert can_reuse_photo({}, "hi")


@pytest.mark.parametrize("field,value", [("source_md5", "bad"), ("source_md5", "A" * 24),
                                         ("source_sha256", "A" * 64), ("source_size_bytes", True),
                                         ("source_size_bytes", 0)])
def test_invalid_source_fingerprints_fail_closed(field, value):
    data, metadata = compress_photo(image_bytes())
    metadata[field] = value
    with pytest.raises(ValueError, match="fingerprints"):
        compress_photo(data, existing=entry(data, metadata))


def test_missing_fingerprints_and_false_local_hash_are_rejected():
    data = image_bytes()
    with pytest.raises(ValueError, match="fingerprints"):
        can_reuse_photo({"quality": "lo"}, "lo")
    with pytest.raises(ValueError, match="digest"):
        compress_photo(data, existing={"sha256": "0" * 64})


def test_orientation_metadata_removal_and_no_upscaling():
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "Private photographer"
    exif[37510] = b"Private coordinates"
    data, metadata = compress_photo(image_bytes((120, 80), exif=exif))
    assert (metadata["width"], metadata["height"]) == (80, 120)
    with Image.open(BytesIO(data)) as image:
        assert not image.getexif()
        assert not any(key in image.info for key in ("exif", "xmp", "comment", "icc_profile"))
    assert b"Private" not in data


def test_transparency_is_flattened_on_white():
    data, _ = compress_photo(image_bytes((10, 10), "RGBA", (0, 0, 0, 0)))
    with Image.open(BytesIO(data)) as image:
        assert image.getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.parametrize("data", [b"not an image", image_bytes()[:-30]])
def test_corrupt_images_are_rejected(data):
    with pytest.raises(ValueError):
        compress_photo(data)


@pytest.mark.parametrize("size", [(20001, 1), (10000, 5000)])
def test_oversized_headers_are_rejected_before_pixel_decode(monkeypatch, size):
    # Real PNG header with deliberately absent pixel payload.
    body = struct.pack(">IIBBBBB", *size, 8, 2, 0, 0, 0)
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", body) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    monkeypatch.setattr(Image.Image, "load", lambda *_: pytest.fail("Pixel decode must not run"))
    with pytest.raises(ValueError, match="dimensions"):
        compress_photo(data)
