# hexatrek-exporter

[![CI](https://github.com/realholgi/hexatrek-exporter/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/realholgi/hexatrek-exporter/actions/workflows/ci.yml)

Export HexaTrek stages, alternative routes, POIs and community reports for OsmAnd and Komoot.
Keep original source data and full comments alongside the generated GPXs.

Use the `hexatrek` CLI to collect downloaded sources, apply local summaries and write exports.
It needs no model API key or Komoot credentials.

- OsmAnd: navigation tracks, POIs and colored bivouac overlays.
- Komoot: GPXs with concise POI text, split into sections when needed.

The exporter does not manage Komoot accounts or tours.

## Install

Python 3.11 or later on macOS or Linux is required:

```bash
git clone https://github.com/realholgi/hexatrek-exporter.git
cd hexatrek-exporter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
hexatrek status --format text
```

The runtime dependency is `requests`, used for explicitly requested community refresh and session operations.

To try the exporter without app data, use the [synthetic example](examples/synthetic/README.md).

## Export a stage

First, [copy the downloaded app cache](docs/extract-hexatrek.md).
Point `--cache-path` at the copy's directory containing `CURRENT`.

```bash
hexatrek collect --stage 1 --cache-path /path/to/firestore-cache
hexatrek export --stage 1 --target osmand
hexatrek status --stage 1 --format text
```

Find the GPXs in `exports/osmand/stage-01/`.
Main routes and alternatives remain separate.

For Komoot, generate an editable summary file from the active snapshot:

```bash
hexatrek summaries --stage 1 --output ./data/stage-01-summaries.json
```

Replace missing `null` entries with reviewed POI text, then prepare and export:

```bash
hexatrek prepare --stage 1 --summaries-file ./data/stage-01-summaries.json
hexatrek export --stage 1 --target komoot
```

Import the GPXs from `exports/komoot/stage-01/` into Komoot.
These commands use local data. For current community reports, see [refresh](docs/workflow.md#2-refresh-community-reports-when-needed).

## Guides

| Guide | Contents |
| --- | --- |
| [Extract HexaTrek data](docs/extract-hexatrek.md) | Download stages in the app, copy the Firestore cache and check coverage |
| [Synthetic example](examples/synthetic/README.md) | Run the offline workflow and inspect small sample GPXs |
| [Local export workflow](docs/workflow.md) | Collect, refresh, prepare and export stages |
| [Komoot-compatible exports](docs/komoot.md) | Prepared GPXs and local detail archives for an independent importer |
| [Prepared artifact format](docs/prepared-artifacts.md) | Version 1 file handoff between independent projects |
| [Files and naming](docs/storage.md) | Private storage, public exports and artifact names |
| [OsmAnd output](docs/osmand.md) | Navigation tracks, bivouac overlays and POI display |
| [OsmAnd photo findings](docs/osmand-photos.md) | Photo limits, evidence and retest procedure |
| [Credential protection](SECURITY.md) | Local commit checks, CI scanning and exposure recovery |

Back up `data/` and `exports/` separately. The repository ignores both directories.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then use the locked development environment:

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pre-commit install
uv run --frozen --extra dev python -m pytest tests
uv run --frozen --extra dev pre-commit run --all-files --hook-stage manual
uv export --frozen --extra dev --no-emit-project --no-hashes | \
  uv run --frozen --extra dev pip-audit --no-deps --disable-pip --strict -r /dev/stdin
```

`uv.lock` fixes direct and transitive development and runtime dependencies.
CI uses the frozen lock and commit-pinned GitHub Actions.
Dependabot checks Python dependencies and Actions each week.

The tests cover snapshot integrity, route separation, cloud pagination failures,
photo associations, local summary validation, export geometry and workflow recovery.
Live cloud access and OsmAnd rendering require separate checks.

## GitHub and contributions

CI tests Python 3.11 and 3.14 on Linux and macOS, then builds a wheel and checks the installed CLI.
Use synthetic data for bug reports and tests.
Report bugs in [GitHub Issues](https://github.com/realholgi/hexatrek-exporter/issues).
Keep source data and exports out of Git and issue attachments.
See [credential protection](SECURITY.md) for commit checks and exposure recovery.

The source code uses the [MIT license](LICENSE).
