"""Read versioned local artifacts and resolve their referenced files."""

import json
from pathlib import Path

from hexatrek_exporter.text_limits import utf16_units


def read_manifest(path):
    manifest = json.loads(Path(path).expanduser().read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Expected a HexaTrek manifest version 1")
    return manifest


def local_file(directory, name):
    base = Path(directory).resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("Export file must exist inside its manifest directory")
    return path


def display_name(name, identity):
    """Keep complete source names and create word-boundary Komoot aliases."""
    if utf16_units(name) <= 30:
        return name
    for long, short in (("Refuge non Gardé", "Abri"), ("Refuge non gardé", "Abri"),
                        ("Chambre d'hotes", "Ch. d'hôtes"), ("Zone de Bivouac", "Bivouac"),
                        ("Fontaine", "Font."), ("Supermarché", "Superm.")):
        name = name.replace(long, short)
    if utf16_units(name) <= 30:
        return name
    suffix = " ·" + str(identity).split(":")[-1]
    words = name.split()
    while words and utf16_units(" ".join(words) + suffix) > 30:
        words.pop()
    if not words:
        raise ValueError("Cannot create a meaningful Komoot name within 30 UTF-16 units")
    return " ".join(words) + suffix

