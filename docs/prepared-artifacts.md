# Prepared artifact format, version 1

[Project overview](../README.md)

This format describes the exporter's prepared files for independent file-based integrations.
Reading them requires no running exporter process.
The filename is `prepared.json`. Its `schema_version` selects the format.
Consumers reject unsupported schema versions instead of guessing.

## Files to transfer

Transfer `prepared.json` together with every referenced GPX and detail file.
Keep their relative paths intact. Do not transfer an exporter's account credentials or entire source database.
A GPX alone is suitable for manual import but is not the complete prepared-artifact handoff.
Detail archives can contain author details, source text, photos and sensitive access links.
See [credential protection](../SECURITY.md) before transferring them.

```text
prepared.json
stage-01.gpx
stage-01-details.zip
```

## Manifest example

```json
{
  "schema_version": 1,
  "stage_id": 1,
  "summary_language": "de",
  "comments_coverage": "cache-only",
  "routes": [
    {
      "id": "1",
      "source_route_id": "1",
      "name": "Stage 1",
      "gpx": "stage-01.gpx",
      "details": "stage-01-details.zip",
      "description": "Source notes for the tour",
      "aliases": {"1:1:42": "Source"}
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `stage_id` | Positive stage number used in publication record names |
| `summary_language` | Normalized POI summary language tag, such as `de`, `en` or `fr`; omitted in older files means `de` |
| `comments_coverage` | Source coverage label; only `cloud-complete` suppresses the cache-only tour notice |
| `routes` | Prepared main routes, alternatives or adjoining sections |
| `routes[].id` | Stable route or section identity, recorded in publication receipts |
| `routes[].source_route_id` | Original route identity for split sections and legacy stage naming |
| `routes[].name` | Planned-tour title |
| `routes[].gpx` | Relative path to the prepared GPX |
| `routes[].details` | Relative path to a local detail file; the exporter writes a ZIP |
| `routes[].description` | Tour description sent to Komoot |
| `routes[].aliases` | Stable source POI IDs mapped to their prepared waypoint names |

Historical version 1 files can omit `stage_id`.
New preparations write `stage_id` explicitly.
Route IDs must remain stable across updates. Do not infer POI identity from proximity or a shortened name.

GPX and detail paths are relative to the manifest directory.
Keep every referenced file inside that directory when transferring a preparation.
Each route's aliases correspond to the named POIs in its GPX.
Names must fit 30 UTF-16 units and descriptions must fit 250 UTF-16 units.
Each planned tour supports at most 512 named POIs. Producers split larger routes into adjoining sections before publication.

## Optional producer metadata

Producers can retain additional provenance fields. Current exports include:

- `source_manifest`: the original manifest path, for attribution only.
- `source_manifest_sha256`: the SHA-256 of the exact original manifest bytes.
- `photo_mode`: whether local photos were included in detail archives.
- Route `kind`, `section`, `poi_ids` and `source_sha256` fields.

`source_sha256` identifies the original route GPX, not the prepared GPX.
Recipients do not need access to `source_manifest` or the producer's inventory, snapshots or stage state.
Those paths can remain historical after the prepared directory is copied to another machine.

## Content preservation

Prepared GPXs contain the selected concise texts. Original names, complete comments and source bytes remain with the producer's data and detail archives.
The current exporter includes `source.gpx` and `source-manifest.json` without modifying their bytes.
Photos stay local and are not uploaded to planned Komoot tours.

