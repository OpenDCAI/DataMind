"""Collapse identical passages while retaining their distinct source evidence."""
from __future__ import annotations

import hashlib


def unique_passages(chunks, top_k):
    groups = {}
    for chunk in chunks:
        value = chunk.model_dump()
        # Preserve internal whitespace: code and tables can depend on it.
        key = hashlib.sha256(value["text"].strip().encode()).hexdigest()
        meta = value.get("metadata") or {}
        location = {"chunk_id": value["id"], "source": value.get("source"),
                    "source_id": meta.get("source_id"), "source_version": meta.get("source_version")}
        if key not in groups:
            groups[key] = {**value, "source_locations": [], "source_location_count": 0}
        group = groups[key]
        group["source_location_count"] += 1
        if len(group["source_locations"]) < 20:
            group["source_locations"].append(location)
        group["source_locations_truncated"] = group["source_location_count"] > 20
    return list(groups.values())[:top_k]
