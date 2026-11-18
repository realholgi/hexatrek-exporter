"""Compare exported source content without refresh or rendering bookkeeping."""


def _index(records, fields, label):
    indexed = {}
    for record in records:
        identity = tuple(record[field] for field in fields)
        if identity in indexed:
            raise ValueError(f"Duplicate {label} identity: {identity!r}")
        indexed[identity] = record
    return indexed


def _content(manifest):
    result = {key: value for key, value in manifest.items()
              if key not in ("exported_at", "refreshed_at", "pois", "photos")}
    if "source" in result:
        result["source"] = {key: value for key, value in result["source"].items()
                            if key != "comments_retrieved_at"}
    photos = _index(manifest.get("photos", []),
                    ("poi_id", "comment_id", "source_image"), "photo")
    photo_urls = {}
    for (poi_id, comment_id, _), photo in photos.items():
        if photo.get("url"):
            photo_urls.setdefault((poi_id, comment_id), set()).add(photo["url"])
    points = _index(manifest.get("pois", []), ("id",), "POI")
    comments = {}
    normalized_points = {}
    for (poi_id,), point in points.items():
        normalized = {key: value for key, value in point.items()
                      if key not in ("summary", "comments")}
        normalized_comments = {}
        for (comment_id,), comment in _index(point.get("comments", []), ("id",), "comment").items():
            content = dict(comment)
            # OsmAnd rendering appends these URLs to comments in place. The
            # authoritative photo records already account for their content;
            # retain only links not derived from those records.
            extra_urls = sorted(set(content.pop("photo_urls", []))
                                - photo_urls.get((poi_id, comment_id), set()))
            if extra_urls:
                content["photo_urls"] = extra_urls
            normalized_comments[comment_id] = content
            comments[(poi_id, comment_id)] = content
        normalized["comments"] = normalized_comments
        normalized_points[poi_id] = normalized
    result["pois"] = normalized_points
    result["photos"] = photos
    return result, comments, photos, points


def _counts(before, after):
    return {"added": len(after.keys() - before.keys()),
            "changed": sum(before[key] != after[key] for key in before.keys() & after.keys()),
            "removed": len(before.keys() - after.keys())}


def _summary_input(point):
    return (point.get("name", ""), point.get("type", ""), point.get("description", ""),
            sorted((comment["created_at"], comment["text"])
                   for comment in point.get("comments", [])))


def compare_manifests(before, after) -> dict:
    """Return content changes and POIs whose model inputs became stale.

    POIs, comments and photos are identity-indexed, rejecting duplicates instead
    of dropping records. Other sequences, including route geometry, retain order.
    Neither input is modified. Added and removed POIs are conservatively stale.
    """
    old, old_comments, old_photos, old_points = _content(before)
    new, new_comments, new_photos, new_points = _content(after)
    identities = old_points.keys() | new_points.keys()
    stale = sorted(identity[0] for identity in identities
                   if identity not in old_points or identity not in new_points
                   or _summary_input(old_points[identity]) != _summary_input(new_points[identity]))
    return {"changed": old != new,
            "comments": _counts(old_comments, new_comments),
            "photos": _counts(old_photos, new_photos),
            "stale_summaries": stale}
