# Local export workflow

[Project overview](../README.md) | [Artifact format](prepared-artifacts.md) | [Komoot-compatible exports](komoot.md)

The CLI defaults to `./data`, relative to the working directory.
Public GPXs go to the sibling `exports/` directory.
Run commands from this repository root to use its existing data.
For another location, put `--data-root PATH` before the command.
Its public output directory is always `PATH.parent/exports`.

Use downloaded HexaTrek data that you are authorized to access.

For readable stage counts, coverage, preparation state and output paths, run:

```bash
hexatrek status --stage 1 --format text
```

Status defaults to JSON. Use `--format text` for a terminal or `--format json` explicitly in scripts.
For an offline trial without app data, follow the [synthetic example](../examples/synthetic/README.md).

## 1. Collect or adopt a source snapshot

If you do not yet have a cache copy, follow [Extract downloaded HexaTrek data](extract-hexatrek.md) first.
It covers the observed macOS app-container path, a safe database copy and coverage checks.

Collect stages from an explicitly selected Firestore cache:

```bash
hexatrek collect --stage 1 2 --cache-path /path/to/firestore-cache
```

Alternatively, adopt an existing export:

```bash
hexatrek adopt --manifest /path/to/export/manifest.json
```

Snapshots preserve original manifest bytes and source filenames.
Main and alternative routes stay separate.
Cached comments are marked cache-only, not current cloud data.

## 2. Refresh community reports when needed

Refresh uses an existing private Firebase session:

```bash
hexatrek refresh --stage 1 2 --session-file /private/session.json --photo-mode none
```

| Photo mode | Result |
| --- | --- |
| `none` | Refresh reports without photos. This is the workflow default. |
| `links` | Retain sensitive, image-specific HTTPS download URLs without downloading images. |
| `files` | Download original images for offline detail archives. |

Refresh uses an existing session and does not create an account.
If you need a session, explicitly authorize anonymous Firebase signup:

```bash
hexatrek session-login --api-key PUBLIC_FIREBASE_API_KEY --session-file /private/session.json --confirm
```

Use the public Firebase key from the app's `GoogleService-Info.plist`.
Keep the session outside exports and version control in a private, user-owned file with mode `0600`.
The command reuses an existing session rather than creating another user.
An interrupted signup requires inspection before retrying.
Invalid or expired refresh credentials do not trigger a new signup.

Refresh renews ID tokens and saves rotated refresh tokens.
It queries by internal POI ID, including reports from older stage/group assignments.
Pagination failures, denied access and inaccessible requested photos abort the operation.
Refreshing invalidates old summaries so changed reports cannot retain stale short text.

The Python `hexatrek_exporter.hexatrek_refresh.refresh_pois` function also accepts a private raw ID-token `token_file`.
With photos enabled, its default mode is `links`, unlike workflow refresh.
Requests use fixed HexaTrek Google endpoints and refuse redirects, ambient proxies and `.netrc` credentials.
Keep photo access URLs private. See [credential protection](../SECURITY.md) for handling and checks.

## 3. Prepare Komoot summaries

Preparation uses local summary text. It does not call a model or translate text.
When a matching preparation or usable POI summaries are already cached, run:

```bash
hexatrek prepare --stage 1 2
```

For missing or replacement summaries, generate an input file for one stage:

```bash
hexatrek summaries --stage 1 --output ./data/stage-01-summaries.json
```

The command records the active snapshot's exact manifest hash and unique route POI IDs.
It retains existing summaries and uses `null` for missing text.
It leaves unrelated POIs out of the file.
Existing output files, including symlinks, are not overwritten.
Write drafts outside managed `data/stages/` and public `exports/` directories.

Replace every `null` with reviewed text. An empty string deliberately leaves a description blank.
Then prepare the stage:

```bash
hexatrek prepare --stage 1 --summaries-file ./data/stage-01-summaries.json
```

The generated file uses this format after editing:

```json
{
  "schema_version": 1,
  "source_manifest_sha256": "<SHA-256 written by the summaries command>",
  "summaries": {
    "1:1:42": "Quelle\n2026-11-10: Wasser gemeldet, ungeprüft."
  }
}
```

Each value is the complete description to write, including any labels, dates and line breaks.
The file can replace cached summaries or supply missing ones.
Every route POI must have text after the supplied and cached summaries are combined.
One summary file applies to one stage. Run separate commands when supplying files for several stages.

Preparation reports all missing, unknown and invalid summaries together. Shared route POIs do not repeat the same error.
Checks include text types, valid XML characters and the 250 UTF-16-unit limit.
Malformed JSON, duplicate keys, unsupported schemas and mismatched source hashes stop validation before text checks.
The exporter writes supplied text without truncation or translation.
Review its factual content, language, dates, closures, water uncertainty and conflicting reports.

Changing the summary file changes the preparation recipe.
Repeating the same validated input reuses the matching preparation.
Source manifests and original comments remain unchanged.

You can instead adopt an existing preparation that matches the active source manifest hash:

```bash
hexatrek adopt-preparation --stage 1 --prepared-path /path/to/prepared.json
```

Preparation preserves source names, coordinates, track geometry and full comments in the original data and detail archives.
Komoot waypoint names use recorded word-boundary aliases within its 30 UTF-16-unit limit.

Routes with more than 512 named POIs split into adjoining Komoot sections.
Sections share boundary track points, and each original POI occurs once at its source position.
Photos stay out of preparations by default.
Use `prepare --include-photos` with a files-mode snapshot for offline photos.

## 4. Export local GPXs

OsmAnd can export directly from a source snapshot:

```bash
hexatrek export --stage 1 2 --target osmand
```

After Komoot preparation, export both destinations:

```bash
hexatrek export --stage 1 2 --target osmand,komoot
hexatrek status --stage 1 2
```

Export writes local GPXs without uploading them.
Matching completed preparations and exports are reused after inventory verification.
Old managed output versions move into internal storage.
Unmanaged public output is not overwritten.
Lower-level filesystem export tools require new destinations unless they explicitly offer an overwrite option.

## Errors and recovery

CLI successes are JSON on stdout. Errors go to stderr with a nonzero exit status.
Multi-stage commands run in order and stop at the first error.
Completed stages are not rolled back.
Inspect status and run records before retrying.
A leftover stage `.lock` needs inspection before manual removal.
Never remove a lock belonging to a running process.

See [Komoot-compatible exports](komoot.md) for manual import and prepared-file transfer.

