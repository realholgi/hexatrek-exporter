# Photos in OsmAnd: findings and retest guide

[Project overview](../README.md) | [OsmAnd output](osmand.md)

Behavior last observed: 2026-11-17. The installed OsmAnd version was not recorded.

## Current decision

OsmAnd GPX exports omit photos and photo URLs by default (`include_photos=False`).
Keep concise POI summaries, report dates, original coordinates and colored bivouac tracks.
Keep original comments, downloaded photos and source manifests privately.
There is no required detail package.

`include_photos=True` opts into the previously tested description HTML output.
It does not enable OsmAnd's **Online-Fotos** gallery.
Revisit this decision after an OsmAnd update, using the procedure below.

## Observed display behavior

A GPX with `<img src="HTTPS_PHOTO_URL">` in its waypoint description loaded a remote image in the full **Beschreibung** window.
A normal GPX `<link type="image/jpeg">` appeared as a website row.
It did not populate **Online-Fotos**.
The waypoint preview exposed raw HTML and long URLs.

Inline Base64 images also worked in the full description.
Current exports omit them because they greatly increase file size.
Base64 adds roughly one third to the image payload before other GPX overhead.

A separate test used a real waypoint with a plain description and `<osmand:osm_tag_image>`.
Its **Online-Fotos** gallery stayed empty.

## Upstream provenance

The earlier investigation inspected official OsmAnd iOS source at commit
`1556562b512436c09c2297d5fed51d3639348751`:

- [GPX waypoint handling](https://github.com/osmandapp/OsmAnd-iOS/blob/1556562b512436c09c2297d5fed51d3639348751/Sources/Models/OAGpxWptItem.mm)
- [GPX waypoint display](https://github.com/osmandapp/OsmAnd-iOS/blob/1556562b512436c09c2297d5fed51d3639348751/Sources/Controllers/TargetMenu/GPX/OAGPXWptViewController.mm)
- [Photo metadata handling](https://github.com/osmandapp/OsmAnd-iOS/blob/1556562b512436c09c2297d5fed51d3639348751/Sources/Controllers/TargetMenu/POI/AmenityExtensionsHelper.swift)
- [Target information display](https://github.com/osmandapp/OsmAnd-iOS/blob/1556562b512436c09c2297d5fed51d3639348751/Sources/Controllers/TargetMenu/OATargetInfoViewController.mm)

Source inspection suggested a missing metadata handoff between GPX waypoints and the gallery.
A URL-encoding probe also raised concerns about nested URLs with escaped paths and query parameters.
Retest both after an update.
For upstream licenses, see the [pinned OsmAnd iOS license](https://github.com/osmandapp/OsmAnd-iOS/blob/1556562b512436c09c2297d5fed51d3639348751/LICENSE).

## Retest after an OsmAnd update

1. Record the installed OsmAnd version and any upstream commit inspected.
2. Use one image that you own or have permission to use.
   Prefer a test URL without private access tokens.
   If authorized HexaTrek access is necessary, follow the [existing-session workflow](workflow.md#2-refresh-community-reports-when-needed).
3. Use one POI with a unique test name and a plain description.
   The following synthetic example tests the previously inspected extension:

   ```xml
   <extensions>
     <osmand:amenity_name>Example photo test</osmand:amenity_name>
     <osmand:osm_tag_image>HTTPS_PHOTO_URL_XML_ESCAPED</osmand:osm_tag_image>
   </extensions>
   ```

   Declare `osmand` as
   `https://osmand.net/docs/technical/osmand-file-formats/osmand-gpx`.
   Do not add HTML images, ordinary photo links or invented OSM/Wikidata IDs to this gallery test.
4. Temporarily hide overlapping older GPX layers.
   OsmAnd can show an old waypoint's text with the newly selected file's group label.
   Verify the unique point name and plain description.
5. Select the GPX waypoint, not the underlying map amenity.
   Expand **Online-Fotos**, wait for loading and verify the actual photograph.
   Restore prior track visibility afterwards.
6. After one image succeeds, test multiple photos, escaped paths, query strings, offline behavior and direct iPhone import.
7. Keep GPXs photo-free until gallery integration is proven and requested.

Keep test artifacts and detailed evidence privately.
