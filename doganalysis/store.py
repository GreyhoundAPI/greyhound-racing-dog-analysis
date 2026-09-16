"""The cache: one compressed file a day per region, never fetched twice once it is complete."""

import gzip
import json
import os
from datetime import timedelta

from .config import DATA, load_json, private_dir, write_json

CACHE = DATA / "cache"
DAY_BYTES_ESTIMATE = 95 * 1024


def region_dir(region):
    return CACHE / region


def day_path(region, day):
    return CACHE / region / ("%04d" % day.year) / ("%s.json.gz" % day.isoformat())


def index_path(region):
    return CACHE / region / "index.json"


def load_index(region):
    data = load_json(index_path(region))
    return data if isinstance(data, dict) else {}


def load_day(region, day):
    try:
        with gzip.open(str(day_path(region, day)), "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, EOFError):
        return None


def save_day(region, day, payload):
    """Write the day atomically, then record it in the index the cost panel reads."""
    path = day_path(region, day)
    private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp")
    with gzip.open(str(temp), "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(str(temp), str(path))
    index = load_index(region)
    index[day.isoformat()] = {
        "complete": bool(payload.get("complete")),
        "races": len(payload.get("races") or []),
        "fields": len(payload.get("fields") or {}),
        "unresolved": (payload.get("results_meta") or {}).get("unresolved") or 0,
        "open": len(payload.get("open") or []),
        "bytes": path.stat().st_size,
    }
    write_json(index_path(region), index)
    return path


def days_between(first, last):
    """Every day from last back to first, newest first."""
    days, day = [], last
    while day >= first:
        days.append(day)
        day -= timedelta(days=1)
    return days


def survey(region, days):
    """What the cache already holds for these days, read from the index alone."""
    index = load_index(region)
    complete, partial, races, fields, empty, size, partial_fields = [], [], 0, 0, [], 0, 0
    for day in days:
        entry = index.get(day.isoformat())
        if not entry:
            continue
        size += entry.get("bytes") or 0
        if entry.get("complete"):
            complete.append(day)
            races += entry.get("races") or 0
            fields += entry.get("fields") or 0
            if not entry.get("races") and not entry.get("unresolved"):
                empty.append(day)
        else:
            partial.append(day)
            partial_fields += entry.get("fields") or 0
    return {
        "complete": complete,
        "partial": partial,
        "races": races,
        "fields": fields,
        "partial_fields": partial_fields,
        "empty": empty,
        "bytes": size,
    }


def average_day_bytes(region):
    sizes = [entry.get("bytes") or 0 for entry in load_index(region).values() if entry.get("races")]
    return int(sum(sizes) / len(sizes)) if sizes else DAY_BYTES_ESTIMATE
