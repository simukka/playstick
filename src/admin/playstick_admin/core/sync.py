"""Push a prepared library into the device's database, and pull its edits back.

WHERE THIS SITS

The preparer runs here and writes an index plus a tree of media next to the
films. The device keeps its own library in SQLite and does not read that index
-- it cannot, the share it would read it from is mounted read-only and the
device is not the machine that made it. So something has to carry one to the
other, and this is it.

Two directions, and they carry different things:

  push()  the film table -- titles, years, genres, and the library-relative
          paths of the film, its re-encode, its subtitles and its per-language
          audio -- plus the poster bytes for films whose poster the device does
          not already have.

  pull_overrides()  the metadata edits somebody made ON the device, written
          back into playstick-overrides.json so the next prep run honours them.

That second one is what makes the device the single home for an edit. It used
to be two homes: a title corrected in the Library tab went into that file and
was invisible to the device until the next prep run, and one corrected in the
Device tab went into a file on the device in a different schema and was
invisible to the preparer forever. Now an edit is made in one place, and this
carries it to the other before a run that would otherwise overwrite it.

WHY POSTERS ARE COMPARED BY ETAG

A prepared library is a few hundred films, and their posters are the only large
thing in the payload. The device reports sha1(bytes)[:16] for each poster it
holds; this hashes the local file the same way and uploads only where they
differ. A re-sync of an unchanged library is therefore one POST and no images
at all, which is the difference between a button that feels instant and one
nobody presses twice.
"""

import hashlib
import os

from .. import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

from . import library_model  # noqa: E402
from . import overrides as overrides_mod  # noqa: E402
from .device_client import DeviceError  # noqa: E402


def poster_etag_of(data):
    """The identity the device computes for these bytes.

    Must stay in step with thumbs.etag_for() on the daemon: the two are
    comparing answers across a network, and a difference in how they are
    derived would mean every poster uploaded on every sync, forever, with
    nothing reporting anything wrong.
    """
    return hashlib.sha1(data).hexdigest()[:16]


def poster_etag(path):
    """The same, for a file. "" when it cannot be read, which makes it differ
    from every real etag and so falls back to trying the upload."""
    try:
        with open(path, "rb") as handle:
            return poster_etag_of(handle.read())
    except OSError:
        return ""


def push(client, output_dir, reconcile=True, progress=None):
    """Send the whole prepared library to the device.

    `progress` is an optional callable taking one sentence, so the Qt layer can
    say what is happening without this module importing any of it.

    Returns a dict: {"written", "removed", "posters", "skipped"}. Raises
    DeviceError if the device is not answering -- the caller shows that; there
    is nothing sensible to do about it here.

    `reconcile` deletes films the device has and this push does not mention,
    which is what a full library sync means. Turn it off to correct one film
    without the device concluding the other ninety-nine were deleted.
    """
    def say(text):
        if progress:
            progress(text)

    index = library_model.load_index(output_dir)
    if not index:
        raise DeviceError("There is no index at %s yet -- run the preparer "
                          "first." % output_dir)
    films = prep.device_films(index)
    say("sending %d film(s)..." % len(films))
    result = client.sync_films(films, reconcile=reconcile)

    # The posters, after the films and never before: a poster is foreign-keyed
    # to its film, so uploading one for a film the device has not been told
    # about yet is a 404 rather than a row.
    say("checking posters...")
    have = {row["id"]: row.get("thumb_etag") or ""
            for row in client.admin_library().get("films") or []}
    uploaded, skipped = 0, 0
    for movie in index.get("movies") or []:
        path = prep.poster_path(output_dir, movie)
        if not path:
            continue
        local = poster_etag(path)
        if not local or local == have.get(movie["id"]):
            skipped += 1
            continue
        with open(path, "rb") as handle:
            client.put_thumb(movie["id"], handle.read())
        uploaded += 1
        say("poster %d: %s" % (uploaded, movie.get("title") or movie["id"]))

    return {
        "written": result.get("written", 0),
        "removed": result.get("removed", 0),
        "posters": uploaded,
        "skipped": skipped,
    }


def pull_overrides(client, output_dir):
    """Write the device's metadata edits into the preparer's overrides file.

    Called before a prep run. Without it the run would derive a title from the
    filename again and push it back over the one somebody fixed on the device
    -- the edit would survive on the device only until the next sync, which is
    exactly the failure the old two-file arrangement had.

    Merges rather than replaces: `tmdb_id` and `note` are the admin app's own
    fields, the device knows nothing about them, and a pull must not be what
    loses a hand-picked TMDb match.

    Returns the number of films whose entry changed.
    """
    device = client.admin_library().get("films") or []
    local = overrides_mod.load(output_dir)
    changed = 0
    for row in device:
        edits = row.get("overrides") or {}
        entry = dict(local.get(row["id"]) or {})
        before = dict(entry)
        # Only the fields the device actually has an edit for. A field it has
        # no override on is a field nobody corrected there, which is not the
        # same as one somebody cleared -- and clearing it here would delete a
        # pin made in this app.
        for key in overrides_mod.SCALAR_FIELDS + overrides_mod.FLAG_FIELDS:
            if key in edits:
                entry[key] = edits[key]
        if not entry.get("source_rel") and row.get("source_rel"):
            entry["source_rel"] = row["source_rel"]
        if entry != before:
            local[row["id"]] = entry
            changed += 1
    if changed:
        overrides_mod.save(output_dir, local)
    return changed


def new_films(source_library, output_dir):
    """Films on the share that nothing has prepared yet. Straight through to
    library_model so the Prepare tab and a sync agree about what "new" is."""
    return library_model.scan_new(source_library, output_dir)


def output_has_index(output_dir):
    return bool(output_dir) and os.path.isfile(
        os.path.join(output_dir, library_model.INDEX_NAME))
