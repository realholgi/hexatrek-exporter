"""Synthetic source comparisons never contact hosted services."""

from copy import deepcopy

import pytest

from hexatrek_exporter.update_content import compare_manifests


ZERO = {"added": 0, "changed": 0, "removed": 0}


@pytest.fixture
def manifest():
    return {
        "schema_version": 1,
        "exported_at": "2026-11-01T00:00:00Z",
        "refreshed_at": "2026-11-01T00:00:00Z",
        "source": {"comments_coverage": "cloud-complete",
                   "comments_retrieved_at": "2026-11-01T00:00:00Z"},
        "routes": [{"id": "1", "poi_ids": ["spring", "shelter"],
                    "points": [{"latitude": 45, "longitude": 6},
                               {"latitude": 46, "longitude": 7}]}],
        "pois": [
            {"id": "spring", "name": "Spring", "type": "Source",
             "description": "Untested water", "summary": "Old summary",
             "comments": [
                 {"id": "a", "created_at": "2026-10-01T12:00:00Z",
                  "text": "Water flowing", "author": "Hiker", "rating": "4/5",
                  "images": ["spring.jpg"], "raw": {"text": "Water flowing"}},
                 {"id": "b", "created_at": "2026-10-02T12:00:00Z",
                  "text": "Access open", "author": "Walker"}]},
            {"id": "shelter", "name": "Shelter", "type": "Refuge",
             "description": "Unstaffed", "comments": []}],
        "photos": [{"poi_id": "spring", "comment_id": "a",
                    "source_image": "spring.jpg", "url": "https://photos.example/spring.jpg"}],
    }


def test_timestamp_and_summary_only_refresh_is_noop_without_mutation(manifest):
    before = deepcopy(manifest)
    after = deepcopy(manifest)
    after["exported_at"] = after["refreshed_at"] = "2026-11-18T12:00:00Z"
    after["source"]["comments_retrieved_at"] = "2026-11-18T12:00:00Z"
    after["pois"][0]["summary"] = "New summary"
    assert compare_manifests(before, after) == {
        "changed": False, "comments": ZERO, "photos": ZERO, "stale_summaries": []}
    assert before == manifest
    assert after["pois"][0]["summary"] == "New summary"


def test_full_comment_edits_additions_and_deletions(manifest):
    after = deepcopy(manifest)
    after["pois"][0]["comments"][0]["raw"]["moderated"] = True
    after["pois"][0]["comments"].pop()
    after["pois"][1]["comments"].append(
        {"id": "a", "created_at": "2026-11-18T00:00:00Z", "text": "Closed"})
    result = compare_manifests(manifest, after)
    assert result == {"changed": True,
                      "comments": {"added": 1, "changed": 1, "removed": 1},
                      "photos": ZERO, "stale_summaries": ["shelter", "spring"]}


@pytest.mark.parametrize("field,value", [
    ("text", "Access closed permanently"),
    ("created_at", "2026-11-18T12:00:00Z"),
])
def test_closure_text_and_date_changes_stale_summary(manifest, field, value):
    after = deepcopy(manifest)
    after["pois"][0]["comments"][1][field] = value
    result = compare_manifests(manifest, after)
    assert result["changed"]
    assert result["comments"] == {"added": 0, "changed": 1, "removed": 0}
    assert result["stale_summaries"] == ["spring"]


@pytest.mark.parametrize("field,value", [
    ("author", "Corrected author"), ("rating", "1/5"),
    ("images", ["different.jpg"]), ("raw", {"other": "metadata"}),
    ("photo_urls", ["https://photos.example/extra.jpg"]),
])
def test_comment_metadata_is_content_but_not_summary_input(manifest, field, value):
    after = deepcopy(manifest)
    after["pois"][0]["comments"][0][field] = value
    result = compare_manifests(manifest, after)
    assert result["changed"]
    assert result["comments"] == {"added": 0, "changed": 1, "removed": 0}
    assert result["stale_summaries"] == []


def test_comment_identity_not_part_of_summary_input(manifest):
    after = deepcopy(manifest)
    after["pois"][0]["comments"][1]["id"] = "replacement"
    result = compare_manifests(manifest, after)
    assert result["comments"] == {"added": 1, "changed": 0, "removed": 1}
    assert result["stale_summaries"] == []


def test_photo_full_record_edits_additions_and_deletions(manifest):
    before = deepcopy(manifest)
    before["photos"].append({"poi_id": "spring", "comment_id": "b",
                             "source_image": "old.jpg", "path": "photos/old.jpg"})
    after = deepcopy(manifest)
    after["photos"][0]["sha256"] = "new-digest"
    after["photos"].append({"poi_id": "spring", "comment_id": "b",
                            "source_image": "new.jpg", "path": "photos/new.jpg"})
    assert compare_manifests(before, after) == {
        "changed": True, "comments": ZERO,
        "photos": {"added": 1, "changed": 1, "removed": 1}, "stale_summaries": []}


def test_renderer_derived_photo_urls_do_not_create_false_refresh_changes(manifest):
    rendered = deepcopy(manifest)
    url = manifest["photos"][0]["url"]
    rendered["pois"][0]["comments"][0]["photo_urls"] = [url, url]
    assert not compare_manifests(rendered, manifest)["changed"]
    assert not compare_manifests(manifest, rendered)["changed"]
    after = deepcopy(manifest)
    after["photos"][0]["url"] = "https://photos.example/new-link.jpg"
    result = compare_manifests(rendered, after)
    assert result == {"changed": True, "comments": ZERO,
                      "photos": {"added": 0, "changed": 1, "removed": 0},
                      "stale_summaries": []}


def test_point_comment_and_photo_order_is_irrelevant(manifest):
    # Two comments with equal dates still have deterministic model fingerprints.
    manifest["pois"][0]["comments"][1]["created_at"] = manifest["pois"][0]["comments"][0]["created_at"]
    manifest["photos"].append({"poi_id": "spring", "comment_id": "b", "source_image": "second.jpg"})
    after = deepcopy(manifest)
    after["pois"][0]["comments"].reverse()
    after["pois"].reverse()
    after["photos"].reverse()
    assert compare_manifests(manifest, after) == {
        "changed": False, "comments": ZERO, "photos": ZERO, "stale_summaries": []}


@pytest.mark.parametrize("field", ["name", "type", "description"])
def test_point_model_input_changes_stale_summary(manifest, field):
    after = deepcopy(manifest)
    after["pois"][0][field] = "Changed"
    assert compare_manifests(manifest, after)["stale_summaries"] == ["spring"]


def test_added_and_deleted_points_are_stale_even_without_comments(manifest):
    after = deepcopy(manifest)
    after["pois"][1]["id"] = "new-shelter"
    result = compare_manifests(manifest, after)
    assert result["changed"]
    assert result["stale_summaries"] == ["new-shelter", "shelter"]


@pytest.mark.parametrize("change", ["geometry", "point-order", "route-order", "coverage", "other-timestamp"])
def test_real_content_and_coverage_changes_are_retained(manifest, change):
    after = deepcopy(manifest)
    if change == "geometry":
        after["routes"][0]["points"][0]["latitude"] = 44
    elif change == "point-order":
        after["routes"][0]["points"].reverse()
    elif change == "route-order":
        manifest["routes"].append({"id": "alternative", "points": []})
        after["routes"] = list(reversed(deepcopy(manifest["routes"])))
    elif change == "coverage":
        after["source"]["comments_coverage"] = "cache-only"
    else:
        after["source"]["other_retrieved_at"] = "2026-11-18T00:00:00Z"
    result = compare_manifests(manifest, after)
    assert result["changed"]
    assert result["stale_summaries"] == []


@pytest.mark.parametrize("kind", ["POI", "comment", "photo"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_duplicate_identities_raise(manifest, kind, side):
    before, after = deepcopy(manifest), deepcopy(manifest)
    target = before if side == "before" else after
    records = (target["pois"] if kind == "POI" else target["photos"] if kind == "photo"
               else target["pois"][0]["comments"])
    records.append(deepcopy(records[0]))
    with pytest.raises(ValueError, match=f"Duplicate {kind} identity"):
        compare_manifests(before, after)
