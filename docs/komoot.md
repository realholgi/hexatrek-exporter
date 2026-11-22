# Komoot-compatible exports

[Project overview](../README.md) | [Local export workflow](workflow.md) | [Artifact format](prepared-artifacts.md)

This repository prepares local files. It does not authenticate with Komoot or change tours.
Import the generated GPXs through Komoot's own import controls.

## Prepare and transfer files

Use the [local workflow](workflow.md) to collect sources and provide validated summaries.
After preparation, `prepared_path` identifies the manifest to pass to an importer.
Transfer that manifest together with all its referenced GPX and detail files.
Keep their relative paths intact.
See the [version 1 artifact format](prepared-artifacts.md) for fields, limits and legacy compatibility.

Public `exports/komoot/stage-NN/` directories contain GPXs only.
They are suitable for manual import but are not a complete prepared-artifact directory.
Komoot exports use GPX 1.1 as the default XML namespace.
If an older export still uses prefixed GPX tags, run `hexatrek export --stage 5 --target komoot` for stage 5.
The command reuses the active preparation and leaves the original files in the source snapshot unchanged.
Keep full preparations under `data/`.

## Offline details

Preparation writes a GPX and detail archive for each route or section.
Each archive contains `komoot.gpx`, lossless `details.json`, readable `index.html` and `komoot-tour-description.txt`.
Original descriptions, source links, authors, ratings and full comments remain in the sidecars.
Comments sort newest first using timezone-aware dates.
Source hashes link archives to their original data.
HTML escapes untrusted text and does not activate unsafe URLs.
Source GPXs and manifests are never overwritten.

Photos stay out of preparations by default.
Use `prepare --include-photos` with a files-mode snapshot when you need offline images.
The Python `hexatrek_exporter.poi_details.add_photos_to_bundle` function also returns new ZIP bytes from verified local JPEG/PNG associations.
Each association needs a detail archive `poi_id`, zero-based newest-first `comment_id` and local photo path.
Optional `source_image` records the original ID or basename, not a signed URL.
Use source metadata to establish associations, not cache hashes, proximity or a visible commenter.
The function preserves image bytes, uses SHA-256 asset names and deduplicates associations.
The total uncompressed archive/photo input limit is 256 MiB.

Planned Komoot tours do not support adding your own photos.
Keep photos and full source comments in local detail archives.

## Scope

The exporter does not manage publication receipts, account credentials or tour updates.
Existing records under `data/publications/komoot/` remain private and unchanged.
