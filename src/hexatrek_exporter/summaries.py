"""Validate explicit local summary text against its exact source manifest."""

import hashlib
import json
from pathlib import Path

from .artifacts import read_manifest
from .text_limits import MAX_DESCRIPTION_UNITS


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _text_issues(identity, text):
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


def summary_document(manifest_path) -> dict:
    """Return editable route summaries bound to the unchanged manifest bytes."""
    manifest_path = Path(manifest_path).expanduser()
    manifest = read_manifest(manifest_path)
    points = {point["id"]: point for point in manifest["pois"]}
    return {
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "summaries": {
            identity: points[identity].get("summary")
            for identity in _route_poi_ids(manifest, points)
        },
    }


def load_summaries(manifest_path, summaries_file=None):
    """Return the source manifest, complete route texts and optional input digest.

    Supplied texts override cached summaries without modifying the source. Every
    supplied text is validated, even if its POI is not used by a route. Text
    issues are reported together; unusable input structures fail immediately.
    """
    manifest_path = Path(manifest_path).expanduser()
    manifest = read_manifest(manifest_path)
    points = {point["id"]: point for point in manifest["pois"]}
    summaries = {identity: point.get("summary") for identity, point in points.items()}
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
            issues.extend(_text_issues(identity, text))
    if issues:
        raise ValueError("\n".join(issues))
    return manifest, summaries, file_sha256
