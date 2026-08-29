"""Read and write playstick-overrides.json.

The preparer honours this file: scalar fields (title, year, ...) win over every
derived source, and a forced tmdb_id makes a hand-picked match bypass the search
and the confidence gate. This module is the writer; src/prep/playstick_prep.py's
load_overrides()/apply_overrides() are the reader, and the two must agree on the
shape below.

    {
      "schema": 1,
      "overrides": {
        "<film id>": {
          "source_rel": "Hook/Hook.mkv",   # for tracing, not used by prep
          "tmdb_id": 2493,                   # forced match (optional)
          "title": "Hook",                   # scalar pins (all optional)
          "sort_title": "hook",
          "year": 1991,
          "rating": 6.8,
          "genres": ["Adventure", "Fantasy"],
          "plot": "...",
          "hidden": false,
          "note": "corrected from Red Hook Summer"
        }
      }
    }
"""

import json
import os
import tempfile

OVERRIDES_NAME = "playstick-overrides.json"
SCHEMA = 1

# Kept in step with playstick_prep.OVERRIDE_FIELDS plus the flags the writer
# owns. A path-like field is deliberately not here: overrides never move files.
SCALAR_FIELDS = ("title", "sort_title", "year", "rating", "genres", "plot")
FLAG_FIELDS = ("hidden",)
META_FIELDS = ("tmdb_id", "source_rel", "note")


def path_for(output_dir):
    return os.path.join(output_dir, OVERRIDES_NAME)


def load(output_dir):
    """Return {film_id: entry}. Missing or unreadable means no overrides."""
    path = path_for(output_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    entries = data.get("overrides") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def save(output_dir, overrides):
    """Write the whole map atomically, dropping empty entries."""
    clean = {fid: entry for fid, entry in overrides.items() if entry}
    payload = {"schema": SCHEMA, "overrides": clean}
    path = path_for(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=output_dir, prefix=".overrides-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_entry(output_dir, film_id, **fields):
    """Merge fields into one film's entry and persist. Passing a field as None
    clears it; other empty values ("" or []) also clear. Returns the entry."""
    overrides = load(output_dir)
    entry = dict(overrides.get(film_id) or {})
    for key, value in fields.items():
        if value in (None, "", []):
            entry.pop(key, None)
        else:
            entry[key] = value
    overrides[film_id] = entry
    save(output_dir, overrides)
    return entry


def clear_entry(output_dir, film_id):
    overrides = load(output_dir)
    if film_id in overrides:
        del overrides[film_id]
        save(output_dir, overrides)
