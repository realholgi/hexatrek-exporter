"""OsmAnd navigation plus independent source-colored bivouac overlays."""

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from hexatrek_exporter.poi_details import GPX, comment_datetime

OSMAND = "https://osmand.net/docs/technical/osmand-file-formats/osmand-gpx"
_COLORS = {"tolerated": "#159447", "forbidden": "#d63131", "unknown": "#777777"}
_LABELS = {"tolerated": "Biwak bedingt geduldet", "forbidden": "Biwak verboten",
           "unknown": "Biwakstatus unbekannt"}

# IDs verified against OsmAnd's public mx_*.svg waypoint resources.
_POI_ICONS = {
    "fontaine": "water_point", "source": "natural_spring", "cours d'eau": "water",
    "restaurant": "amenity_restaurant", "epicerie / artisan": "shop_convenience",
    "supermarché": "shop_supermarket", "toilettes": "amenity_toilets",
    "point de vue": "tourism_viewpoint", "refuge non gardé": "wilderness_hut",
    "refuge gardé": "tourism_alpine_hut", "chambre d'hotes": "tourism_guest_house",
    "camping": "tourism_camp_site", "zone de bivouac": "tourism_camp_site",
    "monument": "monument", "village de france": "village",
    "immanquable": "tourism_attraction", "information": "tourism_information",
}


def _status(category):
    if category in (101, 102):
        return "tolerated"
    if category in (201, 202):
        return "forbidden"
    return "unknown"


def _bivouac_description(manifest, points):
    blocks = [
        "Biwakstatus nicht aktuell rechtlich überprüft.",
        "Grün bedeutet bedingt geduldet, nicht allgemeine Campingerlaubnis. "
        "Rot bedeutet verboten; Grau bedeutet unbekannt und ist keine Erlaubnis.",
        "Originalbedingungen der im gewählten Abschnitt vorkommenden Quellkategorien:",
    ]
    point_types = {record["internalId"]: record for record in manifest.get("point_types", [])}
    for category in sorted({point["category"] for point in points[:-1]}):
        record = point_types.get(category, {})
        lines = [f"Kategorie {category}: {_LABELS[_status(category)]}"]
        for field in ("name", "description"):
            for language, text in record.get(field, {}).items():
                lines.append(f"[{language}] {text}")
        if not record.get("description"):
            lines.append("Originalbedingungen nicht verfügbar.")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _freshness(manifest):
    source = manifest.get("source", {})
    coverage = source.get("comments_coverage", "unknown")
    retrieved = source.get("comments_retrieved_at") or manifest.get("refreshed_at")
    if coverage == "cloud-complete" and retrieved:
        date = comment_datetime(retrieved).strftime("%d.%m.%Y")
        status = f"Kommentare abgerufen am {date}; vor Ort nicht aktuell überprüft."
    else:
        status = "Aktualität nicht bestätigt."
    incomplete = []
    if coverage != "cloud-complete":
        incomplete.append("Kommentare")
    if source.get("photos_coverage") != "cloud-complete":
        incomplete.append("Fotos")
    if incomplete:
        status += " " + " und ".join(incomplete) + " können fehlen."
    return status


def _gpx(name, segments, color, width, description, pois=(), include_photos=False):
    ET.register_namespace("", GPX)
    ET.register_namespace("osmand", OSMAND)
    root = ET.Element(f"{{{GPX}}}gpx", version="1.1", creator="HexaTrek OsmAnd export")
    metadata = ET.SubElement(root, f"{{{GPX}}}metadata")
    ET.SubElement(metadata, f"{{{GPX}}}name").text = name
    ET.SubElement(metadata, f"{{{GPX}}}desc").text = description
    for poi in pois:
        point = ET.SubElement(root, f"{{{GPX}}}wpt", lat=str(poi["latitude"]), lon=str(poi["longitude"]))
        ET.SubElement(point, f"{{{GPX}}}name").text = poi["name"]
        source_type = poi["type"].strip().casefold().replace("’", "'")
        summary = poi.get("summary")
        text = summary if summary is not None else poi.get("description", "")
        comments = sorted(poi.get("comments", []), key=lambda c: comment_datetime(c["created_at"]), reverse=True)
        rich_text = include_photos and any(comment.get("photo_urls") for comment in comments)
        paragraphs = [("<p>" + escape(text).replace("\n", "<br>") + "</p>") if rich_text else text] if text else []
        for comment in comments:
            date = comment_datetime(comment["created_at"]).strftime("%d.%m.%Y")
            if summary is None:
                paragraphs.append(("<p><b>" + date + "</b><br>"
                                   + escape(comment["text"]).replace("\n", "<br>") + "</p>")
                                  if rich_text else date + "\n" + comment["text"])
            for url in comment.get("photo_urls", []) if include_photos else ():
                caption = "Foto zur Meldung vom " + date + "<br>" if summary is not None else ""
                paragraphs.append('<p>' + caption + '<img src="' + escape(url, quote=True)
                                  + '" style="max-width:100%;height:auto"/></p>')
        if summary is not None and comments:
            date = "Letzte Meldung: " + comment_datetime(comments[0]["created_at"]).strftime("%d.%m.%Y")
            paragraphs.append(("<p>" + date + "</p>") if rich_text else date)
        ET.SubElement(point, f"{{{GPX}}}desc").text = ("" if rich_text else "\n\n").join(paragraphs)
        for url in poi.get("links", []):
            if url.startswith(("https://", "http://")):
                ET.SubElement(point, f"{{{GPX}}}link", href=url)
        ET.SubElement(point, f"{{{GPX}}}type").text = poi["type"]
        style = ET.SubElement(point, f"{{{GPX}}}extensions")
        ET.SubElement(style, f"{{{OSMAND}}}icon").text = _POI_ICONS.get(source_type, "tourism_information")
        ET.SubElement(style, f"{{{OSMAND}}}background").text = "circle"
        ET.SubElement(style, f"{{{OSMAND}}}color").text = (
            "#237bc1" if source_type in ("fontaine", "source", "cours d'eau") else "#b77820")
    track = ET.SubElement(root, f"{{{GPX}}}trk")
    ET.SubElement(track, f"{{{GPX}}}name").text = name
    ET.SubElement(track, f"{{{GPX}}}desc").text = description
    # iOS can fall back to per-<trk> colors even when file-level appearance is
    # recognized. Set both explicitly; do not rely on web/Cloud appearance state.
    for parent in (root, track):
        extensions = ET.SubElement(parent, f"{{{GPX}}}extensions")
        ET.SubElement(extensions, f"{{{OSMAND}}}color").text = color
        ET.SubElement(extensions, f"{{{OSMAND}}}coloring_type").text = "solid"
        ET.SubElement(extensions, f"{{{OSMAND}}}width").text = str(width)
        ET.SubElement(extensions, f"{{{OSMAND}}}show_start_finish").text = "false"
    for segment in segments:
        block = ET.SubElement(track, f"{{{GPX}}}trkseg")
        for point in segment:
            node = ET.SubElement(block, f"{{{GPX}}}trkpt", lat=str(point["latitude"]), lon=str(point["longitude"]))
            ET.SubElement(node, f"{{{GPX}}}ele").text = str(point["elevation"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export_osmand(manifest_path: str, output_directory: str, route_id: str, start_m: float = 0, end_m: float | None = None, include_photos: bool = False) -> dict:
    """Write photo-free GPXs by default; remote description photos are opt-in.

    No fake speed/elevation encodings. Every edge has its source-start-point
    category; disjoint runs remain separate segments and are never connected.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    route = next(r for r in manifest["routes"] if str(r["id"]) == str(route_id))
    points = [p for p in route["points"] if p["distance"] >= start_m and (end_m is None or p["distance"] <= end_m)]
    if len(points) < 2:
        raise ValueError("Selected route interval contains fewer than two source points")
    groups = {key: [] for key in _COLORS}
    current, run = None, []
    for a, b in zip(points, points[1:]):
        status = _status(a["category"])
        if status != current:
            if run:
                groups[current].append(run)
            current, run = status, [a]
        run.append(b)
    if run:
        groups[current].append(run)
    identities = set(route["poi_ids"])
    pois = [p for p in manifest["pois"] if p["id"] in identities]
    if len(points) != len(route["points"]):
        # Retain the spatial selection for explicitly restricted test intervals.
        low_lat, high_lat = min(p["latitude"] for p in points), max(p["latitude"] for p in points)
        low_lon, high_lon = min(p["longitude"] for p in points), max(p["longitude"] for p in points)
        pois = [p for p in pois if low_lat - .002 <= p["latitude"] <= high_lat + .002
                and low_lon - .002 <= p["longitude"] <= high_lon + .002]
    poi_by_id = {p["id"]: p for p in pois}
    for photo in manifest.get("photos", []) if include_photos else ():
        poi = poi_by_id.get(photo["poi_id"])
        if poi is None:
            continue
        url = photo.get("url")
        if not url:
            raise ValueError("Selected photo has no URL; refresh with photo_mode='links'")
        if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                                           for char in url) or "\\" in url:
            raise ValueError("Photo URL must be HTTPS with a host and no userinfo or control characters")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme == "https" and bool(parsed.hostname) and "@" not in parsed.netloc
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Photo URL must be HTTPS with a host and no userinfo or control characters")
        comment = next((c for c in poi.get("comments", []) if c["id"] == photo["comment_id"]), None)
        if comment is None:
            raise ValueError("Selected photo comment_id does not match a comment on its POI")
        comment.setdefault("photo_urls", []).append(url)
    output = Path(output_directory).absolute()
    if output.exists():
        raise FileExistsError("OsmAnd export directory already exists")
    exported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    freshness = _freshness(manifest)
    description = freshness + "\n\n" + _bivouac_description(manifest, points)
    staging = Path(tempfile.mkdtemp(prefix=".osmand-export-", dir=output.parent))
    try:
        # Photo metadata remains in the source; GPXs omit images unless opted in.
        (staging / "navigation.gpx").write_bytes(
            _gpx(route["name"], [points], "#3159bc", 3, description, pois, include_photos))
        files = ["navigation.gpx"]
        for status, segments in groups.items():
            if segments:
                name = status + ".gpx"
                (staging / name).write_bytes(_gpx(_LABELS[status], segments, _COLORS[status], 10, description))
                files.append(name)
        summary = {"source_manifest": str(Path(manifest_path).absolute()), "route_id": route_id,
                   "exported_at": exported_at, "source": manifest.get("source", {}),
                   "source_start_m": points[0]["distance"], "source_end_m": points[-1]["distance"],
                   "track_points": len(points), "poi_count": len(pois), "files": files,
                   "segments": {key: len(value) for key, value in groups.items()},
                   "photo_display": "Remote HTML photos require internet; not integrated with Online-Fotos" if include_photos else "disabled",
                   "original_bivouac_conditions": manifest.get("point_types", [])}
        (staging / "export.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        if output.exists():
            raise FileExistsError("OsmAnd destination appeared during export")
        staging.rename(output)
        return {"path": str(output), **summary}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
