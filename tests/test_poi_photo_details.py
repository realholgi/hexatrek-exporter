"""Offline photo preservation and filesystem admission at public interfaces."""

import hashlib
from html.parser import HTMLParser
import io
import json
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

import pytest

from hexatrek_exporter.poi_details import add_photos_to_bundle, details_bundle, extract_poi_details


def source_bundle():
    xml = '''<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
      <wpt lat="49" lon="8"><name>Fontaine &lt;unsafe&gt;</name>
        <desc>User comments (newest first; retrieved today):
2026-11-10T10:00:00Z | Original author | 4/5
Reported water, not tested.</desc><type>Fontaine</type></wpt>
    </gpx>'''
    return details_bundle({"gpx_content": xml,
                           "details": {"source_sha256": "source", "pois": extract_poi_details(ET.fromstring(xml))},
                           "tour_description": "Original source links"})


@pytest.fixture
def photo(tmp_path):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    image = (b"\x89PNG\r\n\x1a\n"
             + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
             + chunk(b"IEND", b""))
    path = tmp_path / "photo.png"
    path.write_bytes(image)
    return path, image


def test_photo_archive_keeps_image_bytes_credit_comments_and_gpx(photo):
    path, image = photo
    source = source_bundle()
    association = {"poi_id": 0, "comment_id": 0, "path": str(path), "source_image": "original.png"}
    result = add_photos_to_bundle(source, [association, association])
    assert result["added_photos"] == result["photo_count"] == result["photo_pois"] == 1
    asset = "photos/" + hashlib.sha256(image).hexdigest() + ".png"
    with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(io.BytesIO(result["content"])) as after:
        assert after.read("komoot.gpx") == before.read("komoot.gpx")
        assert after.read("komoot-tour-description.txt") == before.read("komoot-tour-description.txt")
        assert after.read(asset) == image
        old = json.loads(before.read("details.json"))["pois"][0]
        new = json.loads(after.read("details.json"))["pois"][0]
        assert new["comments"] == old["comments"]
        assert new["description"] == old["description"]
        assert new["photos"][0]["comment_id"] == 0
        html = after.read("index.html").decode()
        assert "Original author" in html and "2026-11-10T10:00:00Z" in html
        assert "Fontaine &lt;unsafe&gt;" in html

        class Images(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sources = []
            def handle_starttag(self, tag, attrs):
                if tag == "img":
                    self.sources.extend(v for k, v in attrs if k == "src")
        parsed = Images()
        parsed.feed(html)
        assert parsed.sources == [asset]
    assert path.read_bytes() == image


@pytest.mark.parametrize("poi_id,comment_id", [(-1, 0), (1, 0), (True, 0), (0, -1), (0, 1), (0, True)])
def test_invalid_photo_association_cannot_target_another_record(photo, poi_id, comment_id):
    with pytest.raises(ValueError, match="source .* ID"):
        add_photos_to_bundle(source_bundle(), [{"poi_id": poi_id, "comment_id": comment_id, "path": str(photo[0])}])


def test_photo_metadata_cannot_copy_signed_urls_into_the_archive(photo):
    with pytest.raises(ValueError, match="not a URL"):
        add_photos_to_bundle(source_bundle(), [{"poi_id": 0, "comment_id": 0, "path": str(photo[0]),
                                               "source_image": "https://example.com/photo.jpg?token=secret"}])


