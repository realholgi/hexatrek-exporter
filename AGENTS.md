# Project rules

## Documentation

- Use STE80. Read `skill://ste80-humanizer` before writing or editing documentation.
- Write short, active sentences. Use plain words and remove repetition.
- Preserve facts, qualifiers, identifiers, commands and paths.
- Keep this file concise. Add large examples only when a rule needs them.
- Avoid defensive disclaimers, repeated warnings and speculative legal caveats. State concrete findings, necessary limits and actionable risks once.

## File names

- Use lowercase ASCII kebab-case and two-digit stage numbers. Use `stage`, never `etappe`, in generated names.
- Include `stage-NN` in every GPX filename. Main routes use `stage-NN.gpx`.
- Split Komoot routes use letter suffixes: `stage-01a.gpx`, `stage-01b.gpx`. Continue after `z` with `aa`, `ab`, etc.
- Omit `hauptstrecke`, `navigation-und-pois`, destination labels, `abschnitt`, `teil` and numeric part suffixes.
- Name alternatives `stage-NN-alternative-<short-name>.gpx`. Split alternatives also use letters.
- Keep these short names:
  - 11: `jura`
  - 21: `dents-blanches`
  - 22: `col-salenton-lac-anterne`
  - 23: `pointe-observatoire`
  - 24: `mont-thabor`
- For new alternatives, use readable title slugs. Resolve collisions with numeric suffixes, not technical IDs.
- OsmAnd overlays append `-biwak` or `-no-biwak` to the route name. `biwak` means conditionally tolerated, not unrestricted permission.
- Omit unknown-status overlays. Preserve complete geometry and source conditions in navigation GPXs and original data.

## Storage

- Put public GPXs in flat `exports/<destination>/stage-NN/` directories. Destinations are `komoot` and `osmand`. Exclude JSONs, ZIPs and metadata.
- Keep internal data under `data/`, beside the public `exports/` root. Dated internal names also use `stage`.
- Prepared GPXs use public Komoot filenames. Matching detail archives append `-details.zip` to the stem.
- Keep shared historical archives as `stage-01-details.zip`.
- Resolve artifacts through `storage.json`. Keep technical IDs in metadata.
- Preserve schema names: `manifest.json`, `prepared.json`, `inventory.json`, `run.json` and `stage.json`.

## Data safety

- Preserve snapshot manifest bytes, original source filenames and original archive members, including `source.gpx` and `source-manifest.json`.
- When renaming, update live references and inventories together. Preserve GPX/ZIP bytes and stable IDs.
- Keep cache reuse working without model calls, network requests or account changes.
