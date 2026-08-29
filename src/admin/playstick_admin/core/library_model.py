"""The library as the admin app sees it: the prepared index merged with local
overrides, plus the source films that have not been prepared yet.

Reuses the preparer's own discovery rules (playstick_prep.discover) so "new"
here means exactly what the next run would treat as a film -- no second, subtly
different idea of what counts.
"""

import json
import os

from .. import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

from . import overrides as overrides_mod  # noqa: E402

INDEX_NAME = "playstick-library.json"


def load_index(output_dir):
    path = os.path.join(output_dir, INDEX_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def load_state(output_dir):
    path = os.path.join(output_dir, prep.WORK_DIR, prep.STATE_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _poster_abspath(output_dir, movie):
    rel = movie.get("poster")
    if not rel:
        return None
    abspath = os.path.join(output_dir, rel.replace("/", os.sep))
    return abspath if os.path.isfile(abspath) else None


def merged_movies(output_dir):
    """Index movies, each annotated for the admin views and with any override
    already applied on top. Returns [] when there is no index yet."""
    index = load_index(output_dir)
    if not index:
        return []
    overrides = overrides_mod.load(output_dir)
    out = []
    for movie in index.get("movies", []):
        item = dict(movie)
        entry = overrides.get(item.get("id"))
        item["has_override"] = bool(entry)
        if entry:
            for key in overrides_mod.SCALAR_FIELDS:
                if entry.get(key) not in (None, "", []):
                    item[key] = entry[key]
            if entry.get("hidden"):
                item["hidden"] = True
            if entry.get("tmdb_id"):
                item["override_tmdb_id"] = entry["tmdb_id"]
        item["poster_abspath"] = _poster_abspath(output_dir, movie)
        item["prepared"] = bool(movie.get("audio") or movie.get("poster")
                                or movie.get("prepared"))
        out.append(item)
    out.sort(key=lambda m: (m.get("sort_title") or m.get("title") or "").lower())
    return out


def scan_new(source_library, output_dir):
    """Video files under the source library that the index and the prep-state
    cache have never seen -- the queue of films waiting to be prepared."""
    if not source_library or not os.path.isdir(source_library):
        return []
    index = load_index(output_dir) or {}
    state = load_state(output_dir)
    known = {m.get("source_rel") for m in index.get("movies", [])}
    known |= set(state.keys())

    args = prep.parse_args(["--library", source_library])
    try:
        found, _episodic = prep.discover(source_library, args)
    except OSError:
        return []

    out = []
    for path in found:
        rel = os.path.relpath(path, source_library).replace(os.sep, "/")
        if rel in known:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        out.append({"rel": rel, "id": prep.file_id(rel), "size": size,
                    "title": prep.clean_title(os.path.basename(rel))})
    out.sort(key=lambda f: f["rel"].lower())
    return out
