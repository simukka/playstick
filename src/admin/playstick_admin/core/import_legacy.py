"""Bringing a library prepared under the old arrangement into this database.

WHAT THE OLD ARRANGEMENT LEFT ON THE SHARE

    playstick-library.json          the film table
    playstick-overrides.json        every metadata correction anybody made
    .playstick/prep-state.json      one ffprobe per film, cached
    .playstick/tmdb-cache.json      what TMDb was asked
    .playstick/posters/<id>.jpg     posters, uploaded from and never read back
    .playstick/{media,subs,audio}/  the films themselves

Only the last line is anything the stick opens. The other five were on the one
filesystem this app is now trying to keep to films, and this reads them into the
database instead.

THE IDS ARE COPIED, NEVER RECOMPUTED

Every film's id comes out of the index verbatim. Three things on the stick hang
off it -- the poster on the grid, every correction, and where each film got to --
and recomputing an id that came out fractionally different would orphan all
three, silently, with a grid that looked perfect afterwards. `film_id` should
agree, because the rule for a file already on the share is deliberately the old
rule; where it does not, that is worth saying and not worth acting on.

NOTHING ON THE SHARE IS DELETED

The import is a read, and nothing here removes the five files it read: the NAS
is somebody's, a rollback needs those files, and an import that deleted its own
source would be an import nobody could check afterwards.
"""

import json
import os
import time

from .. import paths
from . import catalog, identity, prepared

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

INDEX_NAME = "playstick-library.json"
OVERRIDES_NAME = "playstick-overrides.json"


def find_index(output_dir):
    """The old index, or None. Also the test for "is there anything to
    import" -- everything else is optional."""
    if not output_dir:
        return None
    path = os.path.join(output_dir, INDEX_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("movies") else None


def already_imported(db):
    return bool(db.get_meta("imported_from"))


def describe(output_dir):
    """A sentence for the prompt, or None if there is nothing to offer."""
    index = find_index(output_dir)
    if index is None:
        return None
    return ("%s holds a library of %d film(s) prepared by the old arrangement."
            % (output_dir, len(index.get("movies") or [])))


def run(db, output_dir, source_library=None, nas_root=None, report=None):
    """Read the five files into the database. Returns a summary dict.

    Safe to run twice: every write is an upsert keyed by the film's own id, so
    a second run over the same share changes nothing.
    """
    def say(text):
        if report:
            report(text)

    index = find_index(output_dir)
    if index is None:
        return {"films": 0, "reason": "no index at %s" % output_dir}

    nas_root = nas_root or output_dir
    source_library = source_library or output_dir
    source_id = catalog.add_source(db, source_library, label="the share")

    overrides = _load_overrides(output_dir)
    state = _load_state(output_dir)
    movies = index.get("movies") or []
    say("importing %d film(s) from %s" % (len(movies), output_dir))

    imported = posters = mismatched = 0
    for movie in movies:
        ident = movie.get("id")
        source_rel = movie.get("source_rel") or movie.get("rel")
        if not ident or not source_rel:
            continue
        abs_path = os.path.join(source_library,
                                source_rel.replace("/", os.sep))
        computed, nas_rel = identity.film_id(nas_root, abs_path)
        if nas_rel is not None and computed != ident:
            # Said, not acted on. The index's id is what the stick knows.
            mismatched += 1

        catalog.upsert_item(
            db, ident, source_id, source_rel, abs_path,
            nas_rel=nas_rel if nas_rel is not None else source_rel,
            size=0, title=movie.get("title") or "", year=movie.get("year"),
            state=catalog.READY)

        entry = state.get(source_rel) or {}
        cached = entry.get("movie")
        if isinstance(cached, dict):
            catalog.save_probe(
                db, ident, cached,
                catalog.fingerprint(
                    (entry.get("fingerprint") or [0, 0])[0],
                    (entry.get("fingerprint") or [0, 0])[1],
                    entry.get("verify_mode") or "quick",
                    entry.get("meta_version") or prep.META_VERSION))

        poster = _read_poster(output_dir, movie)
        prepared.record(
            db, ident, _as_movie(movie),
            movie.get("rel") or source_rel, source_rel,
            poster=(poster[0], poster[1], movie.get("poster_source") or "import")
            if poster[0] else None)
        if poster[0]:
            posters += 1
        # These films are on the stick already -- that is what an old library
        # means -- so nothing here should offer to push them again.
        prepared.mark_pushed(db, ident)
        imported += 1

    edits = 0
    for ident, entry in overrides.items():
        fields = {k: v for k, v in entry.items()
                  if k in prepared.SCALAR_FIELDS + prepared.FLAG_FIELDS
                  + prepared.META_FIELDS}
        if fields:
            prepared.set_override(db, ident, **fields)
            edits += 1

    db.set_meta("imported_from", output_dir)
    db.set_meta("imported_at", time.time())
    say("imported %d film(s), %d poster(s), %d correction(s)"
        % (imported, posters, edits))
    return {"films": imported, "posters": posters, "overrides": edits,
            "mismatched_ids": mismatched}


def _as_movie(entry):
    """An index entry in the shape prepared.record reads."""
    movie = dict(entry)
    movie["verify_notes"] = entry.get("notes") or []
    movie["phone_audio"] = [
        {"n": t.get("n"), "rel": t.get("rel"), "lang": t.get("lang"),
         "title": t.get("title") or "", "channels": t.get("channels"),
         "default": bool(t.get("default")), "offset": t.get("offset") or 0.0}
        for t in entry.get("audio") or [] if t.get("rel")]
    return movie


def _load_overrides(output_dir):
    path = os.path.join(output_dir, OVERRIDES_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    entries = data.get("overrides") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _load_state(output_dir):
    path = os.path.join(output_dir, prep.WORK_DIR, prep.STATE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_poster(output_dir, movie):
    rel = movie.get("poster")
    if not rel:
        return None, None
    path = os.path.join(output_dir, rel.replace("/", os.sep))
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    try:
        with open(path, "rb") as handle:
            return handle.read(), mime
    except OSError:
        return None, None

