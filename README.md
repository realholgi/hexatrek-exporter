# hexatrek-exporter

Export HexaTrek stages, alternative routes, POIs and community reports for OsmAnd and Komoot.
Keep original source data and full comments alongside the generated GPXs.

Use the `hexatrek` CLI to collect downloaded sources, apply local summaries and write exports.
It needs no model API key or Komoot credentials.

Import the generated GPXs into Komoot manually.
For file-based integrations, transfer `prepared.json` with its referenced GPXs and detail archives.
The exporter does not manage Komoot accounts or tours.

## Install

Python 3.11 or later is required:

```bash
git clone https://github.com/realholgi/hexatrek-exporter.git
cd hexatrek-exporter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
hexatrek status
```

The runtime dependency is `requests`, used for explicitly requested community refresh and session operations.

## Guides

| Guide | Contents |
| --- | --- |
| [Extract HexaTrek data](docs/extract-hexatrek.md) | Download stages in the app, copy the Firestore cache and check coverage |
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
