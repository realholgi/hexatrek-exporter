# Extract downloaded HexaTrek data

[Project overview](../README.md) | [Local export workflow](workflow.md) | [Files and naming](storage.md)

This guide covers the HexaTrek iOS app running on an Apple Silicon Mac.
The container path below was observed locally. Other app versions can use different paths.
The exporter reads an existing Firestore LevelDB cache. It does not extract iPhone backups or Android app storage.

Collect downloaded data with your authorized app access. No Komoot account, model or Firebase session file is needed.

## 1. Download the stages in the app

1. Open HexaTrek and use its normal offline download controls for each required stage.
2. Wait until the app reports that the downloads have finished.
3. Open the required stages and alternatives in the app. Check that their routes and POIs are available.
4. Quit the app normally before copying its database. Closing a window alone may leave the app running.
5. Leave the app closed until the copy is complete.

Map tiles and cached photo files are not the route database.
A completed map download alone does not prove that every route group, alternative or POI is in Firestore.
The exporter cannot download missing route geometry for you.

## 2. Find the Firestore database

In Finder, use **Go > Go to Folder** and enter:

```text
~/Library/Containers/com.hexatrek/Data/Library/Application Support/firestore/
```

The observed database directory is:

```text
~/Library/Containers/com.hexatrek/Data/Library/Application Support/firestore/__FIRAPP_DEFAULT/hexatrek-44801/main/
```

Choose the directory that directly contains `CURRENT`.
If the layout differs, find `CURRENT` below the Firestore directory and inspect its parent directory.
Do not select the whole app container, the Firestore parent directory or one individual `.ldb` file.

A LevelDB directory contains files such as:

```text
CURRENT
MANIFEST-000094
000090.ldb
000096.log
LOCK
```

The numbers change. Do not copy only the numbered files shown here.
`CURRENT` selects the manifest. The manifest selects active `.ldb` or `.sst` tables and the required numeric `.log` files.
`LOG` and `LOG.old` are diagnostic logs, not substitutes for numeric write-ahead logs.

The exporter does not need preferences, Keychain items, purchase receipts or app-session credentials.
If macOS denies access, stop and use a readable copy obtained with your authorized access.

## 3. Copy the complete database directory

Run the following from the repository root, after quitting HexaTrek.
Set `cache` to the database directory you found, not merely to a directory with a similar name.
The example uses the observed path above:

```bash
umask 077
cache="$HOME/Library/Containers/com.hexatrek/Data/Library/Application Support/firestore/__FIRAPP_DEFAULT/hexatrek-44801/main"
snapshot="$PWD/data/source-caches/$(date +%Y-%m-%d-%H%M%S)-stage-cache"
mkdir -p "$PWD/data/source-caches" &&
  mkdir "$snapshot" &&
  cp -R "$cache/." "$snapshot/"
```

Keep using the same shell for the next commands so `$snapshot` retains its value.
Alternatively, replace it with the absolute path to your copy.
`mkdir "$snapshot"` refuses an existing destination, so the command does not merge two database copies.
If copying fails or is interrupted, do not collect that partial copy. Make a fresh copy in a new directory.

Copy the entire database directory, including `CURRENT`, its manifest, tables and numeric logs.
Do not delete lock files, rename database members or run repair or compaction tools on the original.
Keep the copied cache unchanged. Store it privately and back it up separately from Git.
You can reopen HexaTrek after the copy is complete.

## 4. Collect the copied stages

Activate the installed environment if needed:

```bash
source .venv/bin/activate
```

Select only stages that you downloaded. For stages 1 and 2:

```bash
hexatrek --data-root ./data collect --stage 1 2 --cache-path "$snapshot"
hexatrek --data-root ./data status --stage 1 2
```

`collect` reads the copy and creates managed snapshots under `data/stages/`.
It does not write into the app's database, create an account or make network requests.
Each snapshot contains a source manifest and separate GPXs for the main route and cached alternatives.
The manifest preserves original source fields and cached comments.
Multi-stage collection stops at the first error. Earlier successful stages remain stored.

The reader checks LevelDB checksums, record encoding, active-file selection and file changes during reading.
It also rejects missing main-route points, duplicate source identities, invalid coordinates and ambiguous alternative metadata.

## 5. Check coverage before relying on the export

`status` reports route and POI counts, the comment coverage and the managed `snapshot_path` for each stage.
Inspect `manifest.json` in that managed snapshot, not a different historical export.
To list its routes and counts, replace the path in this command:

```bash
python - /path/to/managed-snapshot/manifest.json <<'PY'
import json
from pathlib import Path
import sys

manifest = json.loads(Path(sys.argv[1]).read_text())
print("Stage:", manifest["stage_id"])
print("Comment coverage:", manifest["source"]["comments_coverage"])
print("POIs:", len(manifest["pois"]))
print("Cached comments:", sum(len(poi.get("comments", [])) for poi in manifest["pois"]))
for route in manifest["routes"]:
    print(route["id"], route["kind"], route["name"],
          "track points:", len(route["points"]), "POIs:", len(route["poi_ids"]))
PY
```

Compare the main route, expected alternatives and POI coverage with the app.
An alternative absent from the cache may also be absent from the export without causing a parser error.

For a visual route check, create the local OsmAnd output:

```bash
hexatrek --data-root ./data export --stage 1 2 --target osmand
```

Inspect the generated main and alternative tracks in your GPX viewer.
Check their start and end positions and look for missing sections or unexpected gaps.

`cache-only` comments can be incomplete or outdated. The Firestore copy contains no offline photo archive.
Continue with the [local workflow](workflow.md) to refresh community reports, supply summaries and prepare Komoot artifacts.
See [OsmAnd output](osmand.md) for bivouac conditions and display limits.

## Errors that need a fresh source check

| Error | What to check |
| --- | --- |
| Missing `CURRENT`, manifest or table | Confirm the selected directory and whether the copy contains every database member. |
| `Cache changed during snapshot` or `Cache log inventory changed during snapshot` | Stop using a changing database. Quit the app and make a new complete copy. |
| Checksum, truncated-record or unsupported-encoding error | Keep the failed copy for inspection. Copy again from a closed app. Do not bypass validation or repair the original. |
| `Stage metadata missing or ambiguous` | Confirm the stage number and whether its metadata was downloaded into this cache. |
| `Main stage points not cached` | Return to the app and check the stage download. Map tiles alone are insufficient. |
| Missing alternative metadata or a POI referencing an uncached route | Check that the relevant alternatives and their metadata are available, then make a fresh copy. |
