# Files and naming

[Project overview](../README.md) | [Local export workflow](workflow.md)

```text
.
├── pyproject.toml
├── src/hexatrek_exporter/       # CLI, source readers and export formats
├── tests/
├── docs/                      # Workflow, setup and destination guides
├── data/                       # Private source data and workflow records
│   ├── source-caches/          # Optional private copies of app databases
│   ├── stages/01/
│   │   ├── snapshots/
│   │   ├── preparations/
│   │   ├── exports/            # Internal versions and run.json
│   │   ├── storage.json
│   │   └── stage.json
│   ├── publications/komoot/
│   └── cache/                 # Disposable working files
└── exports/                   # Public GPXs only
    ├── komoot/stage-01/
    │   ├── stage-01a.gpx
    │   ├── stage-01b.gpx
    │   └── stage-01-alternative-jura.gpx
    └── osmand/stage-01/
        ├── stage-01.gpx
        ├── stage-01-biwak.gpx
        ├── stage-01-no-biwak.gpx
        └── stage-01-alternative-jura.gpx
```

The repository ignores `data/` and `exports/`. Back them up separately.
Other storage locations need their own backup and version-control policy.
Keep credentials outside snapshots and exports.

The [extraction guide](extract-hexatrek.md) uses `data/source-caches/` for user-created database copies.
Workflow commands do not manage those copies. Keep them separate from disposable `data/cache/` files.

Public stage directories are flat and contain GPXs only.
Main routes use `stage-NN.gpx`.
Komoot sections append letters: `stage-01a.gpx`, `stage-01b.gpx`, then `aa`, `ab` after `z`.
Alternatives use `stage-NN-alternative-<short-name>.gpx`, with letters for split sections.
Known short names are `jura`, `dents-blanches`, `col-salenton-lac-anterne`, `pointe-observatoire` and `mont-thabor`.
New titles become readable ASCII slugs, with numeric suffixes for collisions.

Internal preparations use the same GPX names and matching `-details.zip` archives.
Historical sections can share one source-route details archive.
Technical IDs remain in metadata.
`storage.json` maps them to readable artifact directories.
Snapshot names use `comments_retrieved_at`, falling back to `exported_at`.
Preparation and export names use their recorded date, stage and purpose.
Repeated directory names receive numeric suffixes such as `-02`.

Keep schema filenames unchanged: `manifest.json`, `prepared.json`, `inventory.json`,
`provenance.json`, `run.json` and `stage.json`.
Preserve original archive members, including `source.gpx` and `source-manifest.json`.
Read route paths from manifests instead of constructing filenames.

