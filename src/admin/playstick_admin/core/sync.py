"""Telling the stick about a film, and asking it what it has been told.

WHERE THIS SITS NOW

It used to be one button. The preparer worked a whole library, wrote an index
onto the share, and this read that index and carried it over in one request --
a step you ran when the hours of ffmpeg had finished.

Films arrive one at a time now. A preparation finishes, its artifacts land on
the share, the admin database records them, and this sends that one film. The
daemon has always supported it: `reconcile` is what a full library push sets,
and without it /api/admin/films is an upsert of exactly what was sent. What is
new is that the payload is built from the admin database rather than from a
file on the share, because there is no longer a file on the share.

WHY A FILM MAY BE PREPARED AND NOT PUSHED, AND WHY THAT IS NOT AN ERROR

A queue that runs for an evening will usually outlast somebody's patience with
leaving the stick switched on. A film whose push failed is still prepared, still
on the share, and still in the database with `pushed_at` unset -- `push_pending`
is the whole recovery, and it costs no ffmpeg. Treating an unreachable stick as
a failed preparation would mean re-encoding a film because a box in another room
was asleep.

RECONCILE IS FOR REPAIR AND NOTHING ELSE

`push_all(reconcile=True)` deletes films the batch does not mention. That is
right after a re-provision, when the stick is empty and this database is the
truth. It is wrong as a way to remove one film, because it would also delete
anything on the stick this database happens not to know about -- which is what
`remove_one` exists for.
"""

import logging
import time

from .. import paths
from . import prepared

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

from .device_client import DeviceError  # noqa: E402

# The daemon refuses a batch larger than this outright. A library that big
# cannot be chunked, because reconcile deletes what a batch does not mention
# and the last chunk would delete every earlier one.
SYNC_MAX = 5000

LOG = logging.getLogger(__name__)


def film_payload(db, ident):
    """One film in the shape POST /api/admin/films wants, or None.

    Assembled into the INDEX shape and then handed to prep.device_film rather
    than written out directly. That indirection is deliberate: device_film is
    where the payload's key names are decided, and a second hand-written copy
    of them here would agree until somebody renamed one.
    """
    row = prepared.get(db, ident)
    if row is None:
        return None
    kids = prepared.children(db, ident)
    return prep.device_film({
        "id": ident,
        "source_rel": row["source_rel"],
        # device_film reads the film to PLAY out of "rel", which is what
        # build_index calls it.
        "rel": row["media_rel"],
        "title": row["title"],
        "sort_title": row["sort_title"],
        "year": row["year"],
        "rating": row["rating"],
        "plot": row["plot"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "vcodec": row["vcodec"],
        "acodec": row["acodec"],
        "tmdb_id": row["tmdb_id"],
        "hidden": bool(row["hidden"]),
        "genres": kids["genres"],
        "series": row["series"],
        "season": row["season"],
        "episode": row["episode"],
        "subtitles": kids["subtitles"],
        "audio": [{"n": t["n"], "rel": t["rel"], "lang": t["lang"],
                   "title": t["title"], "channels": t["channels"],
                   "default": bool(t["is_default"]), "offset": t["offset"]}
                  for t in kids["audio"]],
    })


def push_one(client, db, ident, progress=None):
    """Send one film and its poster. Returns a dict, or raises DeviceError.

    The film first and the poster second, never the other way round: a poster
    is foreign-keyed to its film on the device, so uploading one for a film it
    has not been told about is a 404 rather than a row.
    """
    def say(text):
        if progress:
            progress(text)

    row = prepared.get(db, ident)
    payload = film_payload(db, ident)
    if payload is None:
        raise DeviceError("%s has not been prepared here." % ident)
    # Whether the stick has ever been told about this film, read BEFORE the
    # push marks it. It is what decides if the poster needs comparing at all.
    is_new = row["pushed_at"] is None

    say("telling the stick about %s..." % payload["title"])
    LOG.info("pushing %s (%s)%s", payload["title"], ident,
             "" if is_new else ", which it has heard of before")
    client.sync_films([payload], reconcile=False)

    uploaded = _push_poster(client, db, ident, say, is_new)
    prepared.mark_pushed(db, ident)
    return {"id": ident, "title": payload["title"], "poster": uploaded}


def _push_poster(client, db, ident, say, is_new):
    """Upload this film's poster, unless the device already has these bytes.

    The comparison is what keeps a RE-push cheap: the device names each poster
    by sha1(bytes)[:16] and so does prepared.etag_of, so an unchanged poster is
    one small read and no image data at all.

    It is skipped entirely for a film the stick has never been told about,
    which is the ordinary case here -- every film a queue prepares is new. The
    only way to ask the daemon what posters it holds is to ask for the whole
    film table, so checking would mean fetching a five-hundred-row payload once
    per film to be told each time that it has no poster for a film it heard
    about a second ago.
    """
    row = prepared.poster(db, ident)
    if row is None:
        return False
    if not is_new:
        try:
            have = {f["id"]: f.get("thumb_etag") or ""
                    for f in client.admin_library().get("films") or []}
        except DeviceError:
            # The film is up; the poster is a picture. Worth another go later
            # rather than failing a push that has already done the important
            # half.
            return False
        if have.get(ident) == row["etag"]:
            return False
    say("uploading the poster...")
    LOG.info("uploading the poster for %s: %d bytes", ident, len(row["bytes"]))
    client.put_thumb(ident, row["bytes"], row["mime"])
    return True


def push_pending(client, db, progress=None):
    """Every film that is prepared and that the stick has not been told about.

    The ordinary case is a queue that finished while the stick was switched
    off. Nothing here re-prepares anything.
    """
    def say(text):
        if progress:
            progress(text)

    rows = prepared.unpushed(db)
    LOG.info("%d film(s) prepared here and not yet on the stick", len(rows))
    sent, failed = [], []
    for number, row in enumerate(rows, 1):
        say("[%d/%d] %s" % (number, len(rows), row["title"]))
        try:
            push_one(client, db, row["item_id"])
        except DeviceError as exc:
            LOG.warning("%s did not go: %s", row["title"], exc)
            failed.append((row["title"], str(exc)))
        else:
            sent.append(row["title"])
    LOG.info("pushed %d film(s), %d did not go", len(sent), len(failed))
    return {"sent": len(sent), "failed": failed}


def push_all(client, db, reconcile=True, progress=None):
    """The whole library, in one request. For after a re-provision.

    One request because the daemon writes it as one transaction: a sync that
    fails halfway leaves the library that was there rather than half of a new
    one. Posters follow, and only where the bytes differ.
    """
    def say(text):
        if progress:
            progress(text)

    films, ids = [], []
    for row in prepared.all_prepared(db):
        payload = film_payload(db, row["item_id"])
        if payload is not None:
            films.append(payload)
            ids.append(row["item_id"])
    if len(films) > SYNC_MAX:
        raise DeviceError(
            "%d films is more than the stick accepts in one request (%d), and "
            "a library push cannot be split -- reconcile would delete every "
            "batch but the last." % (len(films), SYNC_MAX))

    say("sending %d film(s)..." % len(films))
    LOG.info("re-sending the whole library: %d film(s), reconcile=%s",
             len(films), bool(reconcile))
    result = client.sync_films(films, reconcile=reconcile)

    say("checking posters...")
    have = {f["id"]: f.get("thumb_etag") or ""
            for f in client.admin_library().get("films") or []}
    uploaded = skipped = 0
    now = time.time()
    for ident in ids:
        row = prepared.poster(db, ident)
        if row is None or have.get(ident) == row["etag"]:
            skipped += 1
        else:
            client.put_thumb(ident, row["bytes"], row["mime"])
            uploaded += 1
            say("poster %d of %d" % (uploaded, len(ids)))
        prepared.mark_pushed(db, ident, now)

    LOG.info("the stick wrote %s film(s) and removed %s; %d poster(s) "
             "uploaded, %d already there", result.get("written", 0),
             result.get("removed", 0), uploaded, skipped)
    return {"written": result.get("written", 0),
            "removed": result.get("removed", 0),
            "posters": uploaded, "skipped": skipped}


def remove_one(client, db, ident):
    """Take one film off the stick.

    A targeted delete rather than push_all(reconcile=True), which would also
    delete every film on the stick this database happens not to know about --
    a second admin machine's work, or anything synced before this database
    existed. That is a great deal of somebody's evening to lose to a tidy-up.
    """
    LOG.info("asking the stick to forget %s", ident)
    client.delete_films([ident])


def pull_overrides(client, db):
    """Copy the stick's metadata edits into the admin database.

    Called before a preparation. Without it a run would derive a title from the
    filename again and push it back over the one somebody fixed from the sofa --
    the edit would survive on the stick only until the next push, which is
    exactly the failure the old two-file arrangement had.

    Merges rather than replaces. A field the device has no override on is a
    field nobody corrected THERE, which is not the same as one somebody
    cleared -- and `tmdb_id` in particular is this app's own idea, which the
    device has no column for and must never be what loses.

    Returns the number of films whose entry changed.
    """
    device = client.admin_library().get("films") or []
    changed = 0
    for row in device:
        edits = row.get("overrides") or {}
        if not edits:
            continue
        ident = row.get("id")
        before = prepared.overrides(db, ident)
        wanted = {k: v for k, v in edits.items()
                  if k in prepared.SCALAR_FIELDS + prepared.FLAG_FIELDS}
        if all(before.get(k) == v for k, v in wanted.items()):
            continue
        prepared.set_override(db, ident, **wanted)
        LOG.info("took %s's edits from the stick: %s", ident,
                 ", ".join(sorted(wanted)))
        changed += 1
    LOG.info("%d film(s) had edits on the stick this machine did not have",
             changed)
    return changed
