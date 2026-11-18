# OsmAnd output

[Project overview](../README.md) | [Local export workflow](workflow.md)

Navigation GPXs retain complete route geometry and all route-associated POIs at their original coordinates, including off-trail POIs.
Separate green and red overlays show known bivouac conditions:

| Source categories | Overlay | Meaning |
| --- | --- | --- |
| `101`, `102` | `-biwak`, green | Conditionally tolerated bivouac, not unrestricted camping permission |
| `201`, `202` | `-no-biwak`, red | Forbidden |
| Unknown, including `301` | No public overlay | No permission inferred |

Omitting unknown overlays does not remove navigation track sections.
The Python `hexatrek_exporter.osmand_export.export_osmand` function retains unknown sections as gray overlays.
GPX metadata and track descriptions preserve complete source conditions in all available source languages.
Disjoint segments stay separate. Elevations are never replaced with permission values.
Partial-route exports use a geographic bounding box with a `0.002`-degree margin, not an along-track access calculation.

Prepared summaries replace repeated description/comment logs where available.
Otherwise, original text remains.
Visible descriptions omit author attribution and retain report dates.
The track description carries one overall freshness note.
Technical timestamps and provenance remain in `export.json`.
Typed POI icons do not establish water safety, access permission or facilities.
Hide older full-route layers if their styling obscures the overlays.

OsmAnd output is photo-free by default, even when its source has photos.
The Python function's explicit `include_photos=True` enables the older description-photo output.
It does not populate OsmAnd's Online-Fotos gallery.
Direct GPX import, overlay colors and typed icons were visually verified in the iOS app running on macOS.
Web/Cloud sync and visibility during active navigation need separate verification.
See [OsmAnd photo findings](osmand-photos.md) for source evidence and the retest guide.

