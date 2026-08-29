"""The library as the admin app sees it, from whichever of the two places knows.

THERE ARE TWO, AND THEY KNOW DIFFERENT THINGS

The DEVICE holds the film table and every metadata edit. It is the truth about
what a film is called, what year it is from and whether the children can see it
-- an edit made in this app lands there, and one made from another copy of this
app is only visible here by asking it.

The PREPARED INDEX on this machine holds what only a prep run can know: whether
a file verified, what the encode came out as, how confident the TMDb match was,
and which films exist that nothing has synced anywhere yet. None of that is on
the device, because none of it is anything the device does.

So `merged_movies` takes the device's rows when there are any and lays the
index's prep facts underneath them. With no device configured -- or one that is
switched off -- it falls back to the index alone merged with the local
overrides file, which is what the app did before the device had a database.
That fallback is not a nicety: preparing a library is most of a day and it
should not require the stick to be plugged in.

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


def merged_movies(output_dir, device_films=None):
    """Every film the app can show, in the shape the views read.

    `device_films` is what GET /api/admin/library answered with, or None when
    there is no device configured or it did not answer. When it is present it
    is the truth about metadata and edits, and the index underneath supplies
    only what a prep run knows and the device does not.

    Each film carries, beyond its own fields:

        source          "device" or "index" -- what the row's metadata came
                        from, so the tab can say which it is showing
        has_override    whether somebody has corrected this film
        override_tmdb_id  a pinned TMDb match. ALWAYS local: the device stores
                        a tmdb_id as a plain column from the sync and has no
                        notion of one having been chosen by hand
        prepared        whether a prep run has actually done the work
        poster_abspath  the poster on THIS machine, if there is one
        thumb_etag      the poster the DEVICE holds, if it has one. Different
                        questions: the device may have extracted a frame for a
                        film this machine has no poster for at all
    """
    index = load_index(output_dir) or {}
    by_id = {m["id"]: m for m in index.get("movies", []) if m.get("id")}
    overrides = overrides_mod.load(output_dir)

    if device_films is None:
        rows = [_from_index(m, overrides, output_dir)
                for m in index.get("movies", [])]
    else:
        rows = [_from_device(row, by_id.get(row.get("id")), overrides,
                             output_dir)
                for row in device_films]
        # Films this machine has prepared that the device has never been told
        # about. Shown rather than hidden, and marked, because "I prepared it
        # and it is not on the stick" is the single most useful thing this tab
        # can tell somebody -- it means the sync has not been run.
        known = {row.get("id") for row in device_films}
        for movie in index.get("movies", []):
            if movie.get("id") not in known:
                item = _from_index(movie, overrides, output_dir)
                item["unsynced"] = True
                rows.append(item)

    rows.sort(key=lambda m: (m.get("sort_title") or m.get("title") or "").lower())
    return rows


def _from_index(movie, overrides, output_dir):
    """One film as the prepared index has it, with the local overrides file
    laid on top. The whole story before the device had a database, and still
    the whole story for a film nothing has synced."""
    item = dict(movie)
    entry = overrides.get(item.get("id"))
    item["source"] = "index"
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
    item["thumb_etag"] = ""
    item["prepared"] = bool(movie.get("audio") or movie.get("poster")
                            or movie.get("prepared"))
    return item


def _from_device(row, movie, overrides, output_dir):
    """One film as the DEVICE has it, over the top of what the index knows.

    /api/admin/library reports the base row and its edits SEPARATELY -- unlike
    /api/library, which serves the merged view a phone sees. That is deliberate
    on the daemon's side and it is what lets this app tell a title somebody
    chose from one the preparer derived, which the pull-back into the overrides
    file depends on.

    It does mean the join is this function's job. Skipping it reads as a film
    marked "edited" still showing the title the edit replaced, which is a
    convincing enough bug to survive being looked at.

    The local overrides file is consulted for one thing only: the pinned TMDb
    id, which is this app's concept and not something the device stores.
    """
    movie = movie or {}
    entry = overrides.get(row.get("id")) or {}
    item = {
        # Prep facts first, so the device's own fields below win where both
        # have an opinion.
        "duration": movie.get("duration"),
        "width": movie.get("width"),
        "height": movie.get("height"),
        "vcodec": movie.get("vcodec"),
        "acodec": movie.get("acodec"),
        "verified": movie.get("verified"),
        "notes": movie.get("notes"),
        "tmdb_score": movie.get("tmdb_score"),
        "metadata_source": movie.get("metadata_source"),
        "poster_source": movie.get("poster_source"),
    }
    item.update({k: v for k, v in row.items() if k != "overrides"})
    # The edits, on top -- the same join Library._hydrate does before answering
    # a phone, so this tab shows what is actually on the grid.
    item.update(row.get("overrides") or {})
    item["source"] = "device"
    # The device's own answer, not the local file's: an edit made from another
    # machine is still an edit, and this tab should say so.
    item["has_override"] = bool(row.get("overrides"))
    item["device_overrides"] = dict(row.get("overrides") or {})
    if entry.get("tmdb_id"):
        item["override_tmdb_id"] = entry["tmdb_id"]
    item["poster_abspath"] = _poster_abspath(output_dir, movie)
    item["prepared"] = bool(movie.get("audio") or movie.get("poster")
                            or movie.get("prepared"))
    return item


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
