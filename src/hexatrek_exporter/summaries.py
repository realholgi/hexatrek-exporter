"""Validate explicit local summary text against its exact source manifest."""

import hashlib
import json
from pathlib import Path

from .artifacts import read_manifest
from .text_limits import MAX_DESCRIPTION_UNITS, utf16_units


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_text(identity, text):
    if not isinstance(text, str):
        raise ValueError(f"Summary for {identity} must be a string")
    if any(not (character in "\t\n\r" or "\u0020" <= character <= "\ud7ff"
                or "\ue000" <= character <= "\ufffd"
                or "\U00010000" <= character <= "\U0010ffff") for character in text):
        raise ValueError(f"Summary for {identity} must contain only valid XML 1.0 text")
    if utf16_units(text) > MAX_DESCRIPTION_UNITS:
        raise ValueError(
            f"Summary for {identity} exceeds {MAX_DESCRIPTION_UNITS} UTF-16 units; it was not truncated")


def load_summaries(manifest_path, summaries_file=None):
    """Return the source manifest, complete route texts and optional input digest.

    Supplied texts override cached summaries without modifying the source. Every
    supplied text is validated, even if its POI is not used by a route.
    """
    manifest_path = Path(manifest_path).expanduser()
    manifest = read_manifest(manifest_path)
    points = {point["id"]: point for point in manifest["pois"]}
    summaries = {identity: point.get("summary") for identity, point in points.items()}
    file_sha256 = None
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
        for identity, text in texts.items():
            if identity not in points:
                raise ValueError(f"Unknown POI summary ID: {identity}")
            _validate_text(identity, text)
        summaries.update(texts)
        file_sha256 = hashlib.sha256(raw).hexdigest()
    for route in manifest["routes"]:
        for identity in route["poi_ids"]:
            if identity not in points:
                raise ValueError(f"Unknown route POI ID: {identity}")
            if summaries[identity] is None:
                raise ValueError(f"Missing prepared summary for {identity}; supply a local summaries file")
            _validate_text(identity, summaries[identity])
    return manifest, summaries, file_sha256
