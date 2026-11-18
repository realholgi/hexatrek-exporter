"""Lossless local preparation and deterministic Komoot-sized route sections."""

from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from hexatrek_exporter.artifacts import display_name, local_file
from hexatrek_exporter.summaries import load_summaries
from hexatrek_exporter.naming import prepared_filenames
from hexatrek_exporter.poi_details import (
    GPX, add_photos_to_bundle, comment_datetime, details_bundle,
    extract_poi_details, tour_source_links,
)

_MAX_POIS = 512


def _sections(tree, identities):
    """Assign each waypoint once, in track order, without moving off-track POIs."""
    waypoints = tree.findall(f"{{{GPX}}}wpt")
    points = tree.findall(f".//{{{GPX}}}trkpt")
    if len(identities) <= _MAX_POIS:
        return [(list(range(len(identities))), 0, len(points) - 1)]
    if not points:
        raise ValueError("Splitting a route requires source GPX track geometry")
    coordinates = [(float(p.get("lat")), float(p.get("lon"))) for p in points]
    located = []
    for ordinal, waypoint in enumerate(waypoints):
        lat, lon = float(waypoint.get("lat")), float(waypoint.get("lon"))
        scale = math.cos(math.radians(lat))
        nearest = min(range(len(points)), key=lambda i:
                      (coordinates[i][0] - lat) ** 2 + ((coordinates[i][1] - lon) * scale) ** 2)
        located.append((nearest, ordinal))
    located.sort()
    groups = [located[i:i + _MAX_POIS] for i in range(0, len(located), _MAX_POIS)]
    boundaries = [0] + [group[0][0] for group in groups[1:]] + [len(points) - 1]
    return [([ordinal for _, ordinal in group], boundaries[i], boundaries[i + 1])
            for i, group in enumerate(groups)]


def _section_tree(tree, ordinals, start, end, split):
    section = deepcopy(tree)
    waypoints = section.findall(f"{{{GPX}}}wpt")
    for point in waypoints:
        section.remove(point)
    # GPX waypoints precede routes and tracks; keep metadata before them.
    insertion = next((i for i, child in enumerate(section)
                      if child.tag in (f"{{{GPX}}}rte", f"{{{GPX}}}trk")), len(section))
    for offset, ordinal in enumerate(ordinals):
        section.insert(insertion + offset, waypoints[ordinal])
    if split:
        index = 0
        for track in section.findall(f"{{{GPX}}}trk"):
            for segment in track.findall(f"{{{GPX}}}trkseg"):
                for point in segment.findall(f"{{{GPX}}}trkpt"):
                    if not start <= index <= end:
                        segment.remove(point)
                    index += 1
                if not segment.findall(f"{{{GPX}}}trkpt"):
                    track.remove(segment)
            if not track.findall(f"{{{GPX}}}trkseg"):
                section.remove(track)
    return section


def _comment_index(point, source_comments, identity):
    """Resolve photo comments against actual GPX ordering, not manifest ordering."""
    ordered = sorted(point.get("comments", []),
                     key=lambda c: (comment_datetime(c["created_at"]), c["id"]), reverse=True)
    available = list(enumerate(source_comments))
    for comment in ordered:
        match = next(((index, original) for index, original in available
                      if comment_datetime(original["created_at"]) == comment_datetime(comment["created_at"])
                      and original["text"] == comment.get("text", "").strip("\r\n")
                      and original["author"] == comment.get("author", "").rstrip()
                      and original["rating"] == comment.get("rating", "").rstrip()), None)
        if match is None:
            raise ValueError("Manifest photo comments do not match original source GPX")
        available.remove(match)
        if comment["id"] == identity:
            return match[0]
    raise ValueError(f"Unknown photo comment {identity!r}")


def prepare_export(manifest_path, output_directory, summaries_file=None, *, include_photos=False):
    """Write a new preparation from complete, validated local summary text.

    Sections share their boundary track point, never their waypoints. Source GPX
    bytes are retained in every detail archive, including original text and links.
    Photos are explicitly omitted by default; include_photos requires local files.
    """
    manifest_path = Path(manifest_path).expanduser()
    manifest, summaries, _ = load_summaries(manifest_path, summaries_file)
    stage = manifest.get("stage_id")
    if stage is None:
        main = next((route for route in manifest["routes"] if route["kind"] == "main"), {})
        identity = str(main.get("source_route_id") or main.get("id", ""))
        if not identity.isdecimal():
            raise ValueError("Preparation requires manifest.stage_id or a numeric main source route ID")
        stage = int(identity)
    if type(stage) is not int or stage < 1:
        raise ValueError("Preparation stage_id must be a positive integer")
    destination = Path(output_directory).expanduser().absolute()
    if destination.exists():
        raise FileExistsError("Prepared export destination already exists")
    root = manifest_path.parent
    point_index = {p["id"]: p for p in manifest["pois"]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".hexatrek-prepared-", dir=destination.parent))
    prepared_routes = []
    try:
        for route in manifest["routes"]:
            source = local_file(root, route["gpx"])
            source_bytes = source.read_bytes()
            source_text = source_bytes.decode("utf-8")
            original_tree = ET.fromstring(source_text)
            if original_tree.tag != f"{{{GPX}}}gpx":
                raise ValueError("Expected a GPX 1.1 document")
            original_points = original_tree.findall(f"{{{GPX}}}wpt")
            identities = route["poi_ids"]
            if len(original_points) != len(identities) or len(set(identities)) != len(identities):
                raise ValueError("Manifest/GPX POI identities differ")
            for identity, point in zip(identities, original_points):
                embedded_id = point.findtext("{http://www.topografix.com/GPX/1/1}extensions/{urn:hexatrek-exporter:hexatrek:1}poi_id")
                if embedded_id is not None and embedded_id != identity:
                    raise ValueError("Manifest/GPX POI identities differ")
            tree = deepcopy(original_tree)
            for identity, waypoint in zip(identities, tree.findall(f"{{{GPX}}}wpt")):
                node = waypoint.find(f"{{{GPX}}}desc")
                if node is None:
                    node = ET.SubElement(waypoint, f"{{{GPX}}}desc")
                node.text = summaries[identity]
            original_details = extract_poi_details(original_tree)
            sections = _sections(original_tree, identities)
            split = len(sections) > 1
            section_routes = [
                dict(route, id=f"{route['id']}-part-{number}" if split else route["id"],
                     source_route_id=route["id"], section={"number": number, "count": len(sections)})
                for number in range(1, len(sections) + 1)
            ]
            naming_routes = []
            for source_route in manifest["routes"]:
                naming_routes.extend(section_routes if source_route is route else [source_route])
            filenames = prepared_filenames(stage, naming_routes)
            for number, (ordinals, start, end) in enumerate(sections, 1):
                section = _section_tree(tree, ordinals, start, end, split)
                section_ids = [identities[i] for i in ordinals]
                aliases, used = {}, set()
                for identity, waypoint in zip(section_ids, section.findall(f"{{{GPX}}}wpt")):
                    name = waypoint.findtext(f"{{{GPX}}}name", default="")
                    alias = display_name(name, identity)
                    if alias in used:
                        alias = display_name(name + " ·" + identity.split(":")[-1], identity)
                    if alias in used:
                        raise ValueError("Komoot waypoint aliases are ambiguous")
                    used.add(alias)
                    node = waypoint.find(f"{{{GPX}}}name")
                    if node is None:
                        node = ET.SubElement(waypoint, f"{{{GPX}}}name")
                    node.text = alias
                    aliases[identity] = alias
                details = {"source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                           "source": deepcopy(manifest["source"]),
                           "source_pois": [deepcopy(point_index[identity]) for identity in section_ids],
                           "source_photos": [deepcopy(photo) for photo in manifest.get("photos", [])
                                             if photo["poi_id"] in section_ids],
                           "pois": [dict(deepcopy(original_details[i]), id=j)
                                    for j, i in enumerate(ordinals)]}
                prepared = {"gpx_content": ET.tostring(section, encoding="unicode", xml_declaration=True),
                            "details": details, "tour_description": tour_source_links(details)}
                archive = details_bundle(prepared)
                photos = []
                for ordinal, identity in enumerate(section_ids if include_photos else []):
                    source_comments = details["pois"][ordinal]["comments"]
                    for photo in manifest.get("photos", []):
                        if photo["poi_id"] != identity:
                            continue
                        if "path" not in photo:
                            raise ValueError("Komoot detail archives require refresh photo_mode='files'; photo links were not discarded")
                        comment_index = _comment_index(point_index[identity], source_comments, photo["comment_id"])
                        photos.append({"poi_id": ordinal, "comment_id": comment_index,
                                       "path": str(local_file(root, photo["path"])),
                                       "source_image": photo["source_image"]})
                if photos:
                    archive = add_photos_to_bundle(archive, photos)["content"]
                buffer = io.BytesIO(archive)
                with zipfile.ZipFile(buffer, "a", compression=zipfile.ZIP_DEFLATED) as bundle:
                    bundle.writestr("source.gpx", source_bytes)
                    bundle.writestr("source-manifest.json", manifest_path.read_bytes())
                names = filenames[section_routes[number - 1]["id"]]
                gpx_name, zip_name = names["gpx"], names["details"]
                (staging / gpx_name).parent.mkdir(parents=True, exist_ok=True)
                (staging / gpx_name).write_text(prepared["gpx_content"])
                (staging / zip_name).write_bytes(buffer.getvalue())
                prepared_routes.append({
                    "id": f"{route['id']}-part-{number}" if split else route["id"],
                    "source_route_id": route["id"], "kind": route["kind"],
                    "name": route["name"] + (f" · {number}/{len(sections)}" if split else ""),
                    "section": {"number": number, "count": len(sections),
                                "start_trackpoint": start, "end_trackpoint": end},
                    "poi_ids": section_ids, "gpx": gpx_name, "details": zip_name,
                    "aliases": aliases, "source_sha256": details["source_sha256"],
                    "description": prepared["tour_description"],
                })
        index = {"schema_version": 1, "stage_id": stage, "source_manifest": str(manifest_path.absolute()),
                 "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                 "photo_mode": "local-files" if include_photos else "omitted",
                 "comments_coverage": manifest["source"]["comments_coverage"], "routes": prepared_routes}
        (staging / "prepared.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
        if destination.exists():
            raise FileExistsError("Prepared export destination appeared during preparation")
        staging.rename(destination)
        return {"prepared_path": str(destination / "prepared.json"), "routes": len(prepared_routes),
                "pois": len(manifest["pois"]), "comments_coverage": index["comments_coverage"]}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
