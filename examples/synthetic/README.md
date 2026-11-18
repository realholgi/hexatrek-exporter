# Synthetic offline example

Run these commands from the repository root after installing `hexatrek`.
All coordinates, descriptions, comments and authors are fictional.
The route lies near 0,0. Do not use these files for navigation.
The commands use local files only.

## Create isolated exports

Use a new temporary directory so the example cannot change existing stage data:

```bash
demo=$(mktemp -d)
hexatrek --data-root "$demo/data" adopt \
  --manifest examples/synthetic/source/manifest.json
hexatrek --data-root "$demo/data" summaries --stage 1 \
  --output "$demo/stage-01-summaries.json"
```

The generated JSON contains the source hash and all three POI IDs.
Its missing summaries are `null` because the source has no summaries.
Replace each `null` with your text, or copy the completed example:

```bash
cp examples/synthetic/stage-01-summaries.json "$demo/stage-01-summaries.json"
hexatrek --data-root "$demo/data" prepare --stage 1 \
  --summaries-file "$demo/stage-01-summaries.json"
hexatrek --data-root "$demo/data" export --stage 1 --target osmand,komoot
hexatrek --data-root "$demo/data" status --stage 1 --format text
```

Without `--format text`, status returns JSON.

The public GPXs land in:

- `$demo/exports/komoot/stage-01/stage-01.gpx`
- `$demo/exports/osmand/stage-01/stage-01.gpx`
- `$demo/exports/osmand/stage-01/stage-01-biwak.gpx`
- `$demo/exports/osmand/stage-01/stage-01-no-biwak.gpx`

Internal snapshots, preparations and detail archives stay under `$demo/data/`.
Only the public GPXs are included in `expected/`.

## Sample contents

`source/manifest.json` uses schema version 1.
`source/stage-01.gpx` was generated with `write_manifest_export`.
The completed summary file binds to the exact manifest bytes.
Each summary is shorter than 250 UTF-16 units.

The samples in `expected/` came from `Workflow.adopt_snapshot`, `Workflow.prepare` and `Workflow.export`.
They use the completed summaries and omit photos.
The source export time is fixed at `2026-01-03T12:00:00Z`.
All three synthetic comments use `2026-01-02T12:00:00Z`.
The example uses `cache-only` comment coverage.

The source and both navigation GPXs have three waypoints and five track points.
Track points have latitude `0`, `0.001`, `0.002`, `0.003`, `0.004`, longitude `0`, and elevations `10` through `14`.
Source distances are `0`, `111`, `222`, `333`, `444` metres.

Category `101` covers the first two edges.
The green bivouac overlay has one segment with points 0, 1 and 2.
Category `201` covers the last two edges.
The red no-bivouac overlay has one segment with points 2, 3 and 4.
Both overlays have no waypoints.
They share the boundary point and cover all four edges once.
No unknown-status overlay is produced.
