"""Readable names are deterministic, bounded and independent of technical IDs."""

import pytest

from hexatrek_exporter.naming import (
    export_name, preparation_name, prepared_filenames, snapshot_name,
)


TIMESTAMP = "2026-11-18T16:30:00+01:00"


def test_snapshot_names_use_report_date_with_export_fallback():
    manifest = {"exported_at": TIMESTAMP, "source": {"comments_coverage": "cache-only"}}
    assert snapshot_name(manifest) == "2026-11-18-app-cache"
    manifest["source"].update(comments_coverage="cloud-complete",
                              comments_retrieved_at="2026-11-17T23:00:00Z")
    assert snapshot_name(manifest) == "2026-11-17-community-aktualisiert"
    del manifest["source"]["comments_retrieved_at"]
    assert snapshot_name(manifest) == "2026-11-18-community-aktualisiert"


def test_preparation_and_export_names_use_supplied_timestamp():
    assert preparation_name(2, {"routes": []}, TIMESTAMP) == "2026-11-18-stage-02-komoot-vorbereitet"
    assert export_name(2, ["osmand", "komoot"], TIMESTAMP) == "2026-11-18-stage-02-komoot-osmand"
    assert export_name(2, ["komoot", "osmand"], TIMESTAMP) == "2026-11-18-stage-02-komoot-osmand"
    assert export_name(1, ["osmand"], TIMESTAMP) == "2026-11-18-stage-01-osmand"


def test_prepared_filenames_include_sections_only_when_required():
    routes = [
        {"id": "main", "kind": "main", "name": "Main", "section": {"count": 1, "number": 1}},
        {"id": "alt-part-1", "kind": "alternative", "name": "Les Crêtes · 1/2"},
        {"id": "alt-part-2", "kind": "alternative", "name": "Les Crêtes · 2/2"},
        {"id": "other", "kind": "alternative", "name": "Vallée", "section": {"count": 2, "number": 3}},
    ]
    names = prepared_filenames(2, routes)
    assert names["main"] == {"gpx": "stage-02.gpx", "details": "stage-02-details.zip"}
    assert names["alt-part-1"]["gpx"] == "stage-02-alternative-cretesa.gpx"
    assert names["alt-part-2"]["details"] == "stage-02-alternative-cretesb-details.zip"
    assert names["other"]["gpx"] == "stage-02-alternative-valleec.gpx"


@pytest.mark.parametrize("stage", [0, -1, True, "1", None])
def test_invalid_stages_fail_clearly(stage):
    with pytest.raises(ValueError, match="positive integer"):
        prepared_filenames(stage, [])


@pytest.mark.parametrize("timestamp", [None, "", "not-a-date", "2026-13-18"])
def test_invalid_timestamps_fail_clearly(timestamp):
    with pytest.raises(ValueError, match="ISO timestamp"):
        preparation_name(1, {}, timestamp)


def test_unknown_coverage_and_missing_section_numbers_fail_clearly():
    with pytest.raises(ValueError, match="comments coverage"):
        snapshot_name({"exported_at": TIMESTAMP, "source": {"comments_coverage": "partial"}})
    with pytest.raises(ValueError, match="section number"):
        prepared_filenames(1, [{"id": "1", "kind": "main", "name": "Main", "section": {"count": 2}}])


def test_historical_split_routes_keep_one_shared_source_details_archive():
    routes = [
        {"id": f"1-part-{number}", "source_route_id": "1", "kind": "main",
         "name": f"Stage 1 · {number}/2", "details": "stage-1-details.zip",
         "section": {"number": number, "count": 2}}
        for number in (1, 2)
    ]
    names = prepared_filenames(1, routes)
    assert {row["details"] for row in names.values()} == {"stage-01-details.zip"}
    assert names["1-part-1"]["gpx"] == "stage-01a.gpx"
    assert names["1-part-2"]["gpx"] == "stage-01b.gpx"


def test_details_archives_cannot_be_shared_across_source_routes():
    routes = [
        {"id": "main", "kind": "main", "name": "Main", "details": "shared.zip"},
        {"id": "alternative", "kind": "alternative", "name": "Crêtes", "details": "shared.zip"},
    ]
    with pytest.raises(ValueError, match="shared across source routes"):
        prepared_filenames(1, routes)
