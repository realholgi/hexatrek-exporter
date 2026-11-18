"""Readable filesystem names; technical identities remain in manifests."""

from datetime import datetime
import re
import unicodedata


_MAX_SLUG = 80
_SECTION_SUFFIX = re.compile(r"\s*·\s*\d+/\d+\s*$")
_PART_SUFFIX = re.compile(r"-part-(\d+)$")


def _stage(stage):
    if type(stage) is not int or stage < 1:
        raise ValueError("Stage must be a positive integer")
    return f"stage-{stage:02d}"


def _date(timestamp):
    if not isinstance(timestamp, str):
        raise ValueError("Artifact naming requires an ISO timestamp")
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError as error:
        raise ValueError("Artifact naming requires an ISO timestamp") from error


def _slug(title):
    title = _SECTION_SUFFIX.sub("", str(title)).strip().lower()
    title = re.sub(r"^(?:(?:le|la|les|un|une|des)\s+|l['’])", "", title)
    title = title.translate(str.maketrans({"œ": "oe", "æ": "ae", "ß": "ss", "ø": "o", "ł": "l"}))
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    title = re.sub(r"[^a-z0-9]+", "-", title).strip("-")
    return title[:_MAX_SLUG].rstrip("-") or "route"


def _source_id(route):
    return str(route.get("source_route_id") or _PART_SUFFIX.sub("", str(route["id"])))


def snapshot_name(manifest):
    """Name source snapshots by report date and coverage, never content hashes."""
    source = manifest["source"]
    coverage = source.get("comments_coverage")
    if coverage == "cache-only":
        label = "app-cache"
    elif coverage == "cloud-complete":
        label = "community-aktualisiert"
    else:
        raise ValueError("Snapshot naming requires cache-only or cloud-complete comments coverage")
    timestamp = source.get("comments_retrieved_at") or manifest.get("exported_at")
    return f"{_date(timestamp)}-{label}"


def preparation_name(stage, prepared, timestamp):
    """Name a preparation using the caller's recorded creation timestamp."""
    return f"{_date(timestamp)}-{_stage(stage)}-komoot-vorbereitet"


def export_name(stage, targets, timestamp):
    """Name an export independently of target input order."""
    names = sorted({_slug(target) for target in targets})
    if not names:
        raise ValueError("Export naming requires at least one target")
    return f"{_date(timestamp)}-{_stage(stage)}-{'-'.join(names)}"


def prepared_filenames(stage, routes):
    """Use the public GPX names internally, retaining full details separately."""
    routes = list(routes)
    public = export_filenames(stage, routes, "komoot")
    archive_sources, archive_counts = {}, {}
    for route in routes:
        archive = route.get("details")
        if not archive:
            continue
        identity = _source_id(route)
        if archive in archive_sources and archive_sources[archive] != identity:
            raise ValueError("A details archive cannot be shared across source routes")
        archive_sources[archive] = identity
        archive_counts[archive] = archive_counts.get(archive, 0) + 1
    names = {}
    for route in routes:
        gpx = public[route["id"]]["gpx"]
        stem = gpx.removesuffix(".gpx")
        section = _public_section(route)
        shared = archive_counts.get(route.get("details"), 0) > 1
        archive_stem = stem.removesuffix(section) if shared and section else stem
        names[route["id"]] = {"gpx": gpx, "details": archive_stem + "-details.zip"}
    return names


_PUBLIC_ALTERNATIVES = {
    "11": "jura",
    "21": "dents-blanches",
    "22": "col-salenton-lac-anterne",
    "23": "pointe-observatoire",
    "24": "mont-thabor",
}


def _public_section(route):
    section = route.get("section") or {}
    part = _PART_SUFFIX.search(str(route["id"]))
    if section.get("count", 1) <= 1 and not part:
        return ""
    number = section.get("number", int(part.group(1)) if part else None)
    if type(number) is not int or number < 1:
        raise ValueError("Split route naming requires a positive section number")
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("a") + remainder) + letters
    return letters


def export_filenames(stage, routes, target):
    """Map route IDs to flat public GPX names, excluding unknown overlays.

    OsmAnd keys describe eligible artifacts; callers publish only those actually
    generated. Internal preparation names and details archives are unchanged.
    """
    _stage(stage)
    if target not in {"komoot", "osmand"}:
        raise ValueError("Public export naming requires komoot or osmand")
    routes = list(routes)
    groups = {}
    for route in routes:
        groups.setdefault(_source_id(route), []).append(route)
    stage_prefix = f"stage-{stage:02d}"
    prefixes, used = {}, set()
    # Reserve approved names first, even when a future title precedes them.
    ordered = sorted(groups, key=lambda identity: (
        groups[identity][0]["kind"] != "main" and identity not in _PUBLIC_ALTERNATIVES
    ))
    for identity in ordered:
        group = groups[identity]
        route = group[0]
        if route["kind"] == "main":
            base = stage_prefix
        else:
            slug = _PUBLIC_ALTERNATIVES.get(identity) or _slug(route["name"])
            base = f"{stage_prefix}-alternative-{slug}"
        prefix, occurrence = base, 1
        categories = ("",) if target == "komoot" else ("", "-biwak", "-no-biwak")
        suffixes = [_public_section(row) + category for row in group for category in categories]
        while any(prefix + suffix in used for suffix in suffixes):
            occurrence += 1
            prefix = f"{base}-{occurrence}"
        used.update(prefix + suffix for suffix in suffixes)
        prefixes[identity] = prefix
    names = {}
    for route in routes:
        stem = prefixes[_source_id(route)] + _public_section(route)
        if target == "komoot":
            names[route["id"]] = {"gpx": stem + ".gpx"}
        else:
            names[route["id"]] = {
                "navigation.gpx": stem + ".gpx",
                "tolerated.gpx": stem + "-biwak.gpx",
                "forbidden.gpx": stem + "-no-biwak.gpx",
            }
    return names
