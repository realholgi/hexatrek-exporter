"""Full offline POI details preserve chronology and escape active content."""

from html.parser import HTMLParser
import xml.etree.ElementTree as ET

from hexatrek_exporter.poi_details import GPX, extract_poi_details, render_details_html


def gpx(*descriptions):
    root = ET.Element(f"{{{GPX}}}gpx", version="1.1", creator="test")
    for index, text in enumerate(descriptions):
        point = ET.SubElement(root, f"{{{GPX}}}wpt", lat=str(49 + index / 100), lon="8.4")
        ET.SubElement(point, f"{{{GPX}}}name").text = f"POI {index}"
        ET.SubElement(point, f"{{{GPX}}}desc").text = text
    track = ET.SubElement(root, f"{{{GPX}}}trk")
    segment = ET.SubElement(track, f"{{{GPX}}}trkseg")
    point = ET.SubElement(segment, f"{{{GPX}}}trkpt", lat="49", lon="8.4")
    ET.SubElement(point, f"{{{GPX}}}ele").text = "120.5"
    return ET.tostring(root, encoding="unicode")


def enriched_gpx(description):
    root = ET.fromstring(gpx(description))
    point = root.find(f"{{{GPX}}}wpt")
    ET.SubElement(point, f"{{{GPX}}}type").text = "Refuge non Gardé"
    ET.SubElement(point, f"{{{GPX}}}link", href="https://example.com/hut")
    return ET.tostring(root, encoding="unicode")


def test_full_details_sort_timezone_aware_dates_and_escape_active_content():
    source = enriched_gpx(
        "User comments (newest first; retrieved today):\n"
        "2026-11-15T00:30:00Z | Early | 1/5\n<img src=x onerror=alert(1)>\n\n"
        "2026-11-14T23:30:00-02:00 | Late | 5/5\n<script>alert(2)</script>"
    )
    root = ET.fromstring(source)
    ET.SubElement(root.find(f"{{{GPX}}}wpt"), f"{{{GPX}}}link", href="javascript:alert(3)")
    records = extract_poi_details(root)
    assert [c["author"] for c in records[0]["comments"]] == ["Late", "Early"]
    html = render_details_html({"source_sha256": "test", "pois": records})

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
            self.links = []
        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            self.links.extend(v for k, v in attrs if k == "href")

    parsed = Tags()
    parsed.feed(html)
    assert "script" not in parsed.tags and "img" not in parsed.tags
    assert parsed.links == ["#poi-0", "https://example.com/hut"]
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
