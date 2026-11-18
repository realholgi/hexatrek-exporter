"""Validate explicit local summary text against its exact source manifest."""

import hashlib
import json
import re
from pathlib import Path

from .artifacts import read_manifest
from .text_limits import MAX_DESCRIPTION_UNITS


def normalize_language(value) -> str:
    """Return a lowercase language tag without whitespace or unsafe characters."""
    if not isinstance(value, str) or re.fullmatch(
            r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value) is None:
        raise ValueError("Summary language must be a 2–3-letter language tag with optional 2–8-character alphanumeric subtags")
    return value.lower()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def text_issues(identity, text) -> list:
    if not isinstance(text, str):
        return [f"Summary for {identity} must be a string"]
    issues = []
    if any(not (character in "\t\n\r" or "\u0020" <= character <= "\ud7ff"
                or "\ue000" <= character <= "\ufffd"
                or "\U00010000" <= character <= "\U0010ffff") for character in text):
        issues.append(f"Summary for {identity} must contain only valid XML 1.0 text")
    # Count lone surrogates too, so invalid XML does not stop other checks.
    if sum(1 + (ord(character) > 0xffff) for character in text) > MAX_DESCRIPTION_UNITS:
        issues.append(
            f"Summary for {identity} exceeds {MAX_DESCRIPTION_UNITS} UTF-16 units; it was not truncated")
    return issues


def _route_poi_ids(manifest, points):
    identities = {}
    for route in manifest["routes"]:
        for identity in route["poi_ids"]:
            if identity not in points:
                raise ValueError(f"Unknown route POI ID: {identity}")
            identities[identity] = None
    return identities


def summary_document(manifest_path, *, language="de") -> dict:
    """Return editable route summaries bound to the unchanged manifest bytes."""
    language = normalize_language(language)
    manifest_path = Path(manifest_path).expanduser()
    manifest = read_manifest(manifest_path)
    points = {point["id"]: point for point in manifest["pois"]}
    cached_language = normalize_language(manifest.get("summary_language", "de"))
    return {
        "schema_version": 1,
        "summary_language": language,
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "summaries": {
            identity: points[identity].get("summary") if cached_language == language else None
            for identity in _route_poi_ids(manifest, points)
        },
    }


def load_summaries(manifest_path, summaries_file=None, *, language="de"):
    """Return the source manifest, complete route texts and optional input digest.

    Supplied texts override cached summaries without modifying the source. Every
    supplied text is validated, even if its POI is not used by a route. Text
    issues are reported together; unusable input structures fail immediately.
    """
    language = normalize_language(language)
    manifest_path = Path(manifest_path).expanduser()
    manifest = read_manifest(manifest_path)
    points = {point["id"]: point for point in manifest["pois"]}
    cached_language = normalize_language(manifest.get("summary_language", "de"))
    summaries = {identity: point.get("summary") if cached_language == language else None
                 for identity, point in points.items()}
    file_sha256 = None
    route_ids = _route_poi_ids(manifest, points)
    texts = {}
    if summaries_file is not None:
        raw = Path(summaries_file).expanduser().read_bytes()
        supplied = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(supplied, dict) or type(supplied.get("schema_version")) is not int or supplied["schema_version"] != 1:
            raise ValueError("Expected a summary JSON schema_version of 1")
        if supplied.get("source_manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
            raise ValueError("Summary source_manifest_sha256 does not match the source manifest")
        supplied_language = normalize_language(supplied.get("summary_language", "de"))
        if supplied_language != language:
            raise ValueError(
                f"Summary summary_language {supplied_language!r} does not match requested language {language!r}")
        texts = supplied.get("summaries")
        if not isinstance(texts, dict):
            raise ValueError("Summary JSON summaries must be an object")
        summaries.update(texts)
        file_sha256 = hashlib.sha256(raw).hexdigest()
    issues = []
    for identity in dict.fromkeys([*route_ids, *texts]):
        if identity in texts and identity not in points:
            issues.append(f"Unknown POI summary ID: {identity}")
        text = summaries[identity]
        if text is None and identity not in texts:
            issues.append(f"Missing prepared summary for {identity}; supply a local summaries file")
        else:
            issues.extend(text_issues(identity, text))
    if issues:
        raise ValueError("\n".join(issues))
    return manifest, summaries, file_sha256
