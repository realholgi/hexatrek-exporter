"""Public names match the GPX-only layout independently of internal storage."""

from pathlib import PurePosixPath

import pytest

from hexatrek_exporter.naming import export_filenames


@pytest.fixture
def stages():
    return {
        1: [
            {"id": "1", "kind": "main", "name": "Grand Est"},
            {"id": "11", "kind": "alternative", "name": "La Traversée du Jura"},
        ],
        2: [
            {"id": "2", "kind": "main", "name": "Alpes du Nord"},
            {"id": "21", "kind": "alternative", "name": "Tour des Dents Blanches"},
            {"id": "22", "kind": "alternative", "name": "Col de Salenton et Lac d’Anterne"},
            {"id": "23", "kind": "alternative", "name": "La Pointe de l’Observatoire"},
            {"id": "24", "kind": "alternative", "name": "Le Mont Thabor"},
        ],
    }


def test_full_two_stage_komoot_names(stages):
    prepared = [
        {**stages[1][0], "id": f"1-part-{number}", "source_route_id": "1",
         "section": {"number": number, "count": 2}}
        for number in (1, 2)
    ] + stages[1][1:]
    assert export_filenames(1, prepared, "komoot") == {
        "1-part-1": {"gpx": "stage-01a.gpx"},
        "1-part-2": {"gpx": "stage-01b.gpx"},
        "11": {"gpx": "stage-01-alternative-jura.gpx"},
    }
    assert export_filenames(2, stages[2], "komoot") == {
        "2": {"gpx": "stage-02.gpx"},
        "21": {"gpx": "stage-02-alternative-dents-blanches.gpx"},
        "22": {"gpx": "stage-02-alternative-col-salenton-lac-anterne.gpx"},
        "23": {"gpx": "stage-02-alternative-pointe-observatoire.gpx"},
        "24": {"gpx": "stage-02-alternative-mont-thabor.gpx"},
    }


@pytest.mark.parametrize("stage", [1, 2])
def test_full_two_stage_osmand_names(stages, stage):
    names = export_filenames(stage, stages[stage], "osmand")
    expected = {
        1: {"1": "stage-01", "11": "stage-01-alternative-jura"},
        2: {
            "2": "stage-02",
            "21": "stage-02-alternative-dents-blanches",
            "22": "stage-02-alternative-col-salenton-lac-anterne",
            "23": "stage-02-alternative-pointe-observatoire",
            "24": "stage-02-alternative-mont-thabor",
        },
    }[stage]
    assert names == {
        identity: {
            "navigation.gpx": stem + ".gpx",
            "tolerated.gpx": stem + "-biwak.gpx",
            "forbidden.gpx": stem + "-no-biwak.gpx",
        }
        for identity, stem in expected.items()
    }
    assert all("unknown.gpx" not in files for files in names.values())


@pytest.mark.parametrize("number,letters", [(1, "a"), (2, "b"), (26, "z"), (27, "aa"), (52, "az"), (53, "ba")])
def test_partition_letters_from_sections_or_historical_ids(number, letters):
    routes = [
        {"id": "section", "source_route_id": "main", "kind": "main", "name": "Main",
         "section": {"count": 53, "number": number}},
        {"id": f"11-part-{number}", "kind": "alternative", "name": "Jura"},
    ]
    assert export_filenames(1, routes, "komoot") == {
        "section": {"gpx": f"stage-01{letters}.gpx"},
        f"11-part-{number}": {"gpx": f"stage-01-alternative-jura{letters}.gpx"},
    }


def test_unknown_titles_are_safe_unique_and_do_not_steal_approved_names():
    titles = ["La Crête", "Crête", "Crête-2", "../\\\x00", "東京", "É" * 400, "E" * 400, "Jura"]
    routes = [
        {"id": f"technical-{index}", "kind": "alternative", "name": title}
        for index, title in enumerate(titles)
    ] + [{"id": "11", "kind": "alternative", "name": "Changed source title"}]
    names = export_filenames(1, iter(routes), "komoot")
    filenames = [row["gpx"] for row in names.values()]
    assert filenames[:3] == [
        "stage-01-alternative-crete.gpx", "stage-01-alternative-crete-2.gpx",
        "stage-01-alternative-crete-2-2.gpx",
    ]
    assert filenames[3:5] == ["stage-01-alternative-route.gpx", "stage-01-alternative-route-2.gpx"]
    assert names["technical-7"]["gpx"] == "stage-01-alternative-jura-2.gpx"
    assert names["11"]["gpx"] == "stage-01-alternative-jura.gpx"
    assert len(set(filenames)) == len(routes)
    for filename in filenames:
        assert filename.isascii() and len(filename) < 120
        assert PurePosixPath(filename).name == filename
        assert ".." not in filename and "technical" not in filename
        assert filename.endswith(".gpx")


def test_osmand_overlay_names_cannot_collide_with_future_titles():
    routes = [
        {"id": "future", "kind": "alternative", "name": "Jura-biwak"},
        {"id": "11", "kind": "alternative", "name": "Jura"},
    ]
    names = export_filenames(1, routes, "osmand")
    assert names["future"]["navigation.gpx"] == "stage-01-alternative-jura-biwak-2.gpx"
    files = [filename for row in names.values() for filename in row.values()]
    assert len(files) == len(set(files))


@pytest.mark.parametrize("stage", [0, -1, True, "1", None])
def test_invalid_stages_fail_clearly(stage):
    with pytest.raises(ValueError, match="positive integer"):
        export_filenames(stage, [], "komoot")


def test_invalid_targets_and_missing_section_numbers_fail_clearly():
    with pytest.raises(ValueError, match="komoot or osmand"):
        export_filenames(1, [], "unknown")
    with pytest.raises(ValueError, match="positive section number"):
        export_filenames(1, [{"id": "1", "kind": "main", "name": "Main", "section": {"count": 2}}], "komoot")
