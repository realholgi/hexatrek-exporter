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
| `files` | Download and compress local images for offline detail archives. |

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

### Photo quality and private libraries

Local photo downloads use `med` by default.
Choose a preset with `--photo-quality`:

| Preset | Maximum longest side | JPEG quality |
| --- | --- | --- |
| `hi` | 1000 px | 55 |
| `med` | 800 px | 50 |
| `lo` | 640 px | 45 |

The encoder preserves orientation and aspect ratio, avoids upscaling and removes EXIF/GPS metadata.
These are lossy reference images. Fine text and zoomed detail can be lost.
Quality affects local files, not remote photos displayed through links.

Download the active snapshot's photo references into `data/photos/stage-01/`:

```bash
hexatrek photos --stage 1 --session-file /private/session.json --photo-quality med
```

This command does not refresh comments or change active exports.
To compact an existing library offline, omit `--session-file`.
The library keeps SHA-256 filenames and maps each file to its POI, comment and original image name.
It records the original image's MD5, SHA-256 and byte size separately from the local file's SHA-256.
Matching Firebase metadata permits reuse of compressed files without downloading the original again.
Changed objects need new image bytes.
An increase in quality can also require a download when the original pixels are no longer available.
Repeating the same preset reuses the encoded bytes without further quality loss.

Files-mode refreshes and updates also accept `--photo-quality`.
They automatically use a matching private stage library when present:

```bash
hexatrek update --stage 1 --session-file /private/session.json \
  --photo-mode files --photo-quality lo
```

Updates inherit the selected quality from an existing files-mode snapshot, otherwise they use `med`.
The library retains exact source manifests and source GPX bytes.
Compaction validates a complete replacement before removing the previous photo files.


## 3. Prepare Komoot summaries

German (`de`) is the default summary language.
Use `--language en`, `fr`, `es` or another language tag on `summaries` and `prepare` to select a different language.
Drafting and preparation stay offline. [Automatic generation](#automatic-summaries) requires `summaries --generate`.
When a matching preparation or usable POI summaries are already cached, run:

```bash
hexatrek prepare --stage 1 2
```

For missing or replacement summaries, generate an input file for one stage:

```bash
hexatrek summaries --stage 1 --output ./data/stage-01-summaries.json
```

The command records the active snapshot's exact manifest hash, selected `summary_language` and unique route POI IDs.
It retains cached summaries in the selected language and uses `null` for missing text.
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
  "summary_language": "de",
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
Malformed JSON, duplicate keys, unsupported schemas and mismatched source hashes or languages stop validation before text checks.
Preparation writes supplied text without truncation or translation.
Review its factual content, language, dates, closures, water uncertainty and conflicting reports.

Changing the summary file changes the preparation recipe.
Repeating the same validated input reuses the matching preparation.
Source manifests and original comments remain unchanged.

Unlabelled summary files, source summaries and preparations use German.
Preparations are cached separately by language. German text is not reused for another language.
`export` uses the active preparation; preparing another language selects it for the next export.
Source names, full comments and fixed export labels keep their original language.

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

### Automatic summaries

Choose an OpenAI-compatible Chat Completions endpoint with JSON-object response support and a model it serves:

```bash
hexatrek summaries --stage 1 --generate --language en \
  --api-base http://localhost:11434/v1 --model your-model \
  --output ./data/stage-01-summaries-en.json
hexatrek prepare --stage 1 --language en \
  --summaries-file ./data/stage-01-summaries-en.json
hexatrek export --stage 1 --target osmand,komoot
```

Replace `your-model` with your model name. Omit `--language` for German.
The API base can use HTTPS, or HTTP on `localhost`, `127.0.0.1` or `::1`.
No endpoint or model is selected implicitly.
Set `HEXATREK_API_BASE` and `HEXATREK_MODEL` instead of repeating the corresponding flags.
For providers that require authentication, set `HEXATREK_API_KEY` as described in [credential protection](../SECURITY.md#model-credentials).

The model receives POI names, types, descriptions and complete reports with dates.
It generates missing or invalid text in batches of ten POIs.
Valid cached text, including an intentional blank, stays unchanged.
Matching prepared text in the selected language is reused without model requests, even when another language is active.

Every response must contain exactly the requested POI IDs and valid text within 250 UTF-16 units.
Refusals, incomplete replies and provider errors abort generation. There are no automatic retries or silent truncation.
The output file is written only after all batches pass validation.
Existing output files are never overwritten.

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

## 5. Update exported stages

Use `update` to refresh reports and photos, then rebuild the stages you already exported:

```bash
hexatrek update --stage 1 2 --session-file /private/session.json
```

The command inherits each stage's exported targets, active summary language and source photo mode.
German applies when no language is recorded.
Use `--target osmand,komoot`, `--language en` or `--photo-mode links` to change these settings for a new update.
Use `links` for photo URLs in OsmAnd GPXs.
Use `files` for compressed images in private detail archives. Public directories still contain only GPXs.
File reuse requires a valid local image, matching local SHA-256 and matching cloud content metadata.
If the cloud image changed at the same name, the command downloads its new bytes.

Each update reports added, changed and removed comments and photos.
Changes to retrieval timestamps alone do not replace exports.
Unchanged summary inputs retain their reviewed text.
Changes to names, descriptions, types or report text/dates require new summaries.
Photo-only changes do not require new summaries.
An unprepared OsmAnd export keeps full reports without requiring short summaries.

### Review changed summaries

When text needs review, the command returns `status: "needs-review"`, an `update_id` and a private `summaries_path`.
Current exports and the active snapshot stay unchanged.
Edit the reported file. Replace the affected `null` values and retain its hash, language and POI IDs.
Then resume the same candidate:

```bash
hexatrek update --stage 1 --resume UPDATE_ID
```

Replace `UPDATE_ID` with the returned value.
To use a separate edited file, add `--summaries-file ./data/stage-01-reviewed.json`.
Resume uses the saved candidate and needs no session or new cloud fetch.
Its photo mode, quality, targets and language remain fixed.

### Generate changed summaries

With a model endpoint configured as described under [automatic summaries](#automatic-summaries), use:

```bash
hexatrek update --stage 1 --session-file /private/session.json --generate
```

Only missing or invalid summaries go to the model.
You can also add `--generate` when resuming a review.
No model call runs without this flag.

### Publication and recovery

The command builds and validates all requested payloads before replacing current exports.
It archives prior managed versions and preserves filenames, route geometry and elevations.
Publication uses a journal and one directory rename per target.
If publication stops between targets, resume the same update to finish it without fetching, generating or rendering again.
Use `hexatrek status --stage 1 --format text` to find pending update IDs after an error.
A newer stage state or user-edited public export blocks stale candidates.

Updates change local exports. Reimport the GPXs to update copies already in navigation apps.


## Errors and recovery

CLI successes are JSON on stdout. Errors go to stderr with a nonzero exit status.
Multi-stage commands run in order and stop at the first error.
Completed stages are not rolled back.
Inspect status and run records before retrying.
A leftover stage `.lock` needs inspection before manual removal.
Never remove a lock belonging to a running process.

See [Komoot-compatible exports](komoot.md) for manual import and prepared-file transfer.

