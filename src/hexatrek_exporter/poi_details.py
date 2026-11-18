"""Lossless POI sidecars and newest-first comments from our GPX export format."""

from datetime import datetime, timezone
import hashlib
from html import escape
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import zipfile

from hexatrek_exporter.poi_types import get_poi_type

GPX = "http://www.topografix.com/GPX/1/1"
_PHOTO_PATH = re.compile(r"photos/[0-9a-f]{64}\.(?:jpg|png)")
_SOURCE_FILES = {"source.gpx", "source-manifest.json"}
_MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_COMMENT_SECTION = re.compile(r"(?m)^User comments \([^\r\n]*\):[ \t]*\r?\n")
_COMMENT_HEADER = re.compile(
    r"(?m)^(?P<created_at>\d{4}-\d{2}-\d{2}T[^\s|]+)[ \t]*\|[ \t]*"
    r"(?P<author>[^\r\n]*?)[ \t]*\|[ \t]*(?P<rating>[^\r\n]*)\r?$"
)


def comment_datetime(value: str) -> datetime:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Comment dates must include a timezone")
    return date.astimezone(timezone.utc)


def extract_poi_details(root) -> list[dict]:
    records = []
    for index, point in enumerate(root.findall(f"{{{GPX}}}wpt")):
        description = point.findtext(f"{{{GPX}}}desc", default="")
        marker = _COMMENT_SECTION.search(description)
        comments = []
        intro = description
        if marker:
            body = description[marker.end():]
            headers = list(_COMMENT_HEADER.finditer(body))
            if headers:
                intro = description[:marker.start()].rstrip("\r\n")
                for position, header in enumerate(headers):
                    end = headers[position + 1].start() if position + 1 < len(headers) else len(body)
                    comments.append({
                        "created_at": header["created_at"], "author": header["author"].rstrip(),
                        "rating": header["rating"].rstrip(),
                        "text": body[header.end():end].strip("\r\n"),
                    })
                comments.sort(key=lambda item: comment_datetime(item["created_at"]), reverse=True)
        source_type = point.findtext(f"{{{GPX}}}type", default="")
        kind = get_poi_type(source_type)
        records.append({
            "id": index, "name": point.findtext(f"{{{GPX}}}name", default=""),
            "latitude": point.get("lat"), "longitude": point.get("lon"),
            "source_type": source_type, "type_de": kind.de if kind else source_type,
            "type_en": kind.en if kind else source_type,
            "description": description, "intro": intro, "comments": comments,
            "links": [link.get("href", "") for link in point.findall(f"{{{GPX}}}link")],
        })
    return records


def _link(url: str) -> str:
    try:
        parts = urlsplit(url)
        safe = parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password
    except ValueError:
        safe = False
    text = escape(url, quote=True)
    return f'<a href="{text}" rel="noreferrer">{text}</a>' if safe else text


def render_details_html(details: dict) -> str:
    def paragraph(text):
        return "<p>" + escape(text).replace("\n", "<br>\n") + "</p>"

    records = details["pois"]
    parts = ['<!doctype html><html lang="de"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>HexaTrek – vollständige POI-Details</title><body>',
             '<h1>HexaTrek – vollständige POI-Details</h1>',
             paragraph("Originaltexte ohne Kürzung. Kommentare je POI: neueste zuerst. "
                       "Nutzerberichte sind keine Garantie für aktuelle Verhältnisse."),
             paragraph("SHA-256 der Quell-GPX: " + details["source_sha256"]), '<nav><ol>']
    parts.extend(f'<li><a href="#poi-{p["id"]}">{escape(p["name"])}</a></li>' for p in records)
    parts.append('</ol></nav>')
    for point in records:
        parts.extend([f'<article id="poi-{point["id"]}"><h2>{escape(point["name"])}</h2>',
                      paragraph(f'Typ: {point["type_de"]} · Originaltyp: {point["source_type"]}'),
                      paragraph(f'{point["latitude"]}, {point["longitude"]}'),
                      paragraph(point["intro"])])
        parts.extend('<p>' + _link(url) + '</p>' for url in point["links"])
        if point["comments"]:
            parts.append('<h3>Kommentare – neueste zuerst</h3>')
        for comment_id, comment in enumerate(point["comments"]):
            parts.append(paragraph(f'{comment["created_at"]} · {comment["author"]} · {comment["rating"]}'))
            parts.append(paragraph(comment["text"]))
            for photo in point.get("photos", []):
                if photo["comment_id"] != comment_id:
                    continue
                path = photo["path"]
                if not _PHOTO_PATH.fullmatch(path):
                    raise ValueError("Photo assets must use local content-addressed paths")
                parts.append(
                    f'<figure><a href="{path}"><img src="{path}" '
                    f'alt="{escape(point["name"], quote=True)}" loading="lazy" '
                    f'style="max-width:100%;height:auto"></a><figcaption>'
                    f'{escape(comment["author"])} · {escape(comment["created_at"])}'
                    '</figcaption></figure>'
                )
        parts.append('</article>')
    parts.append('</body></html>')
    return '\n'.join(parts)


def tour_source_links(details: dict) -> str:
    lines = ["HexaTrek: ergänzende Quellen und Originaltypen", "",
             "Die POI-Kurztexte enthalten Originaltypen und datierte Nutzerberichte. "
             "Vollständige Kommentare stehen in der separat gespeicherten Detaildokumentation und Original-GPX. "
             "Wasser- und Unterkunftssymbole garantieren weder Trinkbarkeit noch Übernachtungserlaubnis.",
             "", "Weiterführende Quellen:"]
    for point in details["pois"]:
        for url in point["links"]:
            try:
                parsed = urlsplit(url)
                safe = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                safe = False
            if safe:
                lines.append(f'{point["name"]} ({point["type_de"]}): {url}')
    return '\n'.join(lines)


def details_bundle(prepared: dict, assets: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("komoot.gpx", prepared["gpx_content"])
        archive.writestr("details.json", json.dumps(prepared["details"], ensure_ascii=False, indent=2))
        archive.writestr("index.html", render_details_html(prepared["details"]))
        archive.writestr("komoot-tour-description.txt", prepared["tour_description"])
        for path, content in (assets or {}).items():
            if not _PHOTO_PATH.fullmatch(path) and path not in _SOURCE_FILES:
                raise ValueError("Invalid detail archive asset path")
            archive.writestr(path, content)
    return buffer.getvalue()


def add_photos_to_bundle(content: bytes, photos: list[dict]) -> dict:
    """Copy verified comment-photo associations into a new offline archive."""
    fixed = {"komoot.gpx", "details.json", "index.html", "komoot-tour-description.txt"} | _SOURCE_FILES
    assets = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        total = sum(member.file_size for member in members)
        if total > _MAX_BUNDLE_BYTES:
            raise ValueError("Detail archive exceeds 256 MiB uncompressed")
        if len({member.filename for member in members}) != len(members):
            raise ValueError("Duplicate archive member names")
        if any(member.filename not in fixed and not _PHOTO_PATH.fullmatch(member.filename) for member in members):
            raise ValueError("Not a supported POI detail archive; no files were discarded")
        prepared = {
            "gpx_content": archive.read("komoot.gpx").decode("utf-8"),
            "details": json.loads(archive.read("details.json")),
            "tour_description": archive.read("komoot-tour-description.txt").decode("utf-8"),
        }
        for member in members:
            if _PHOTO_PATH.fullmatch(member.filename):
                data = archive.read(member)
                if hashlib.sha256(data).hexdigest() != Path(member.filename).stem:
                    raise ValueError("Photo asset digest does not match its filename")
                assets[member.filename] = data
            elif member.filename in _SOURCE_FILES:
                assets[member.filename] = archive.read(member)
    points = prepared["details"]["pois"]
    added = 0
    for photo in photos:
        point_id = photo.get("poi_id")
        comment_id = photo.get("comment_id")
        if type(point_id) is not int or not 0 <= point_id < len(points) or points[point_id]["id"] != point_id:
            raise ValueError("Unknown source POI ID")
        point = points[point_id]
        if type(comment_id) is not int or not 0 <= comment_id < len(point["comments"]):
            raise ValueError("Unknown source comment ID")
        path = Path(photo["path"]).expanduser()
        total += path.stat().st_size
        if total > _MAX_BUNDLE_BYTES:
            raise ValueError("Detail archive and input photos exceed 256 MiB")
        data = path.read_bytes()
        if data.startswith(b"\xff\xd8\xff"):
            suffix = "jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = "png"
        else:
            raise ValueError("Only JPEG and PNG photo assets are supported")
        digest = hashlib.sha256(data).hexdigest()
        asset = f"photos/{digest}.{suffix}"
        source_image = photo.get("source_image")
        if source_image is not None and (
            not isinstance(source_image, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", source_image)
        ):
            raise ValueError("source_image must be an image ID, not a URL or filesystem path")
        existing = point.setdefault("photos", [])
        if any(item["comment_id"] == comment_id and item["sha256"] == digest for item in existing):
            continue
        association = {"comment_id": comment_id, "path": asset, "sha256": digest}
        if source_image is not None:
            association["source_image"] = source_image
        existing.append(association)
        assets[asset] = data
        added += 1
    # Fail before publication if an existing association references missing data.
    for point in points:
        for photo in point.get("photos", []):
            if photo["path"] not in assets:
                raise ValueError("A referenced photo asset is missing")
    return {"content": details_bundle(prepared, assets), "added_photos": added,
            "photo_count": sum(len(point.get("photos", [])) for point in points),
            "photo_pois": sum(bool(point.get("photos")) for point in points)}
