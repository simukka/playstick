"""The games library on this machine, and getting it onto the stick. Qt-free.

A game takes the film's route, with most of the steps gone. There is nothing to
probe, encode or match, so there is no queue either. Importing a ROM checks its
header (roms.py), copies it onto the share under .playstick/roms/, records it
here, and tells the stick. That is a few kilobytes and a second, so it is done
on the spot.

The copy goes through nas_store.place, the same atomic `.part` and rename a
film's artifacts use, for the same reason: the stick's mount caches attributes
for a minute, and a file written in place could be read at a stale size.

PUSHING IS PER KIND

Games go to the same /api/admin/films route as films, with `kind: "game"` on
the batch. The daemon scopes a reconcile to the batch's kind, so push_all here
can repair the games on the stick without deleting a single film, and the
Library page's film push cannot delete a game.
"""

import hashlib
import logging
import os
import shutil
import time

from .. import paths
from . import nas_store, roms
from .device_client import DeviceError

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

LOG = logging.getLogger(__name__)

# Under prep.WORK_DIR on the share, next to media/, subs/ and audio/. Listed in
# nas_store.OWNED, which is what lets a removal delete a ROM and the orphan
# walk find a stray one.
ROMS_DIR = "roms"

# The fields the Games page may edit. The system and the ROM path come from
# the header and the import, and changing them would make the row lie about
# the file.
EDITABLE = ("title", "year", "players", "plot", "hidden")


def rel_for(rom):
    return "%s/%s/%s%s" % (prep.WORK_DIR, ROMS_DIR, rom.ident, rom.extension)


# -- this machine's table ---------------------------------------------------

def all_games(db):
    return db.query("SELECT * FROM game ORDER BY sort_title, title")


def get(db, ident):
    return db.one("SELECT * FROM game WHERE id = ?", (ident,))


def update(db, ident, fields):
    """Save an edit from the Games page. Unknown keys are dropped."""
    clean = {k: v for k, v in fields.items() if k in EDITABLE}
    if "players" in clean:
        clean["players"] = 2 if clean["players"] == 2 else 1
    if "title" in clean:
        clean["title"] = (clean["title"] or "").strip() or None
        if clean["title"] is None:
            del clean["title"]
        else:
            clean["sort_title"] = roms.sort_title(clean["title"])
    if "hidden" in clean:
        clean["hidden"] = 1 if clean["hidden"] else 0
    if not clean:
        return
    with db.write() as conn:
        conn.execute(
            "UPDATE game SET %s WHERE id = ?"
            % ", ".join("%s = ?" % k for k in clean),
            tuple(clean.values()) + (ident,))


def set_poster(db, ident, data, mime):
    etag = hashlib.sha1(data).hexdigest()[:16]
    with db.write() as conn:
        conn.execute("UPDATE game SET poster_mime = ?, poster_bytes = ?, "
                     "poster_etag = ? WHERE id = ?", (mime, data, etag, ident))
    return etag


def claimed_rels(db):
    """Every ROM path this database knows, for nas_store.orphans."""
    return {r["rel"] for r in db.query("SELECT rel FROM game")}


# -- importing --------------------------------------------------------------

def import_rom(db, nas_root, rom):
    """Copy one checked ROM onto the share and record it. Returns its id.

    The same ROM imported again keeps its row, edits included, and only has
    its source path brought up to date: the id is a hash of the bytes, so it
    is the same game whichever folder it came from.
    """
    if not nas_root:
        raise nas_store.NasError("no share is set -- Config, Settings")
    ident, rel = rom.ident, rel_for(rom)
    staging = nas_store.staging_for(nas_root, ident)
    src = os.path.join(staging, rel.replace("/", os.sep))
    try:
        # makedirs and the write fail as plain OSError -- a share that is not
        # mounted, a full one, a permission -- and the Games page reports a
        # NasError per file where an OSError would end the whole app.
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "wb") as handle:
                handle.write(rom.data)
        except OSError as exc:
            raise nas_store.NasError("could not write to the share: %s"
                                     % exc) from exc
        nas_store.place(nas_root, ident, [rel])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    now = time.time()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO game (id, source_path, rel, system, title, "
            "sort_title, warning, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET source_path = excluded.source_path, "
            "rel = excluded.rel, system = excluded.system, "
            "warning = excluded.warning",
            (ident, rom.path, rel, rom.system, rom.title,
             roms.sort_title(rom.title), rom.warning, now))
    LOG.info("imported %s (%s) as %s", rom.title, rom.system, ident)
    return ident


# -- the stick --------------------------------------------------------------

def payload(row):
    """One game in the shape POST /api/admin/films wants."""
    return {
        "id": row["id"],
        "kind": "game",
        "system": row["system"],
        "source_rel": row["rel"],
        "media_rel": row["rel"],
        "title": row["title"],
        "sort_title": row["sort_title"],
        "year": row["year"],
        "plot": row["plot"],
        "players": row["players"],
        "hidden": bool(row["hidden"]),
    }


def push_one(client, db, ident):
    """Send one game, then its poster. The poster goes second because it is
    foreign-keyed to the row on the stick."""
    row = get(db, ident)
    if row is None:
        raise DeviceError("%s is not in this machine's games." % ident)
    client.sync_films([payload(row)], reconcile=False, kind="game")
    if row["poster_bytes"]:
        client.put_thumb(ident, row["poster_bytes"], row["poster_mime"])
    _mark_pushed(db, [ident])


def push_all(client, db, reconcile=True):
    """Every game, in one request, and the posters the stick lacks.

    With `reconcile`, the stick deletes the games this batch does not name. It
    never touches a film -- the daemon scopes the delete to kind 'game'.
    """
    rows = all_games(db)
    result = client.sync_films([payload(r) for r in rows],
                               reconcile=reconcile, kind="game")
    have = {f["id"]: f.get("thumb_etag") or ""
            for f in client.admin_library().get("films") or []}
    posters = 0
    for row in rows:
        if row["poster_bytes"] and have.get(row["id"]) != row["poster_etag"]:
            client.put_thumb(row["id"], row["poster_bytes"],
                             row["poster_mime"])
            posters += 1
    _mark_pushed(db, [r["id"] for r in rows])
    return {"written": result.get("written", 0),
            "removed": result.get("removed", 0), "posters": posters}


def remove(client, db, nas_root, ident):
    """Off the stick, off the share, and out of this database, in that order.

    The stick goes first so a failure there leaves everything as it was: a
    game still listed here and still playable, rather than a row on the stick
    pointing at a ROM that has been deleted.
    """
    row = get(db, ident)
    if row is None:
        return
    client.delete_films([ident])
    if nas_root:
        nas_store.remove(nas_root, [row["rel"]])
    with db.write() as conn:
        conn.execute("DELETE FROM game WHERE id = ?", (ident,))


def _mark_pushed(db, ids):
    now = time.time()
    with db.write() as conn:
        conn.executemany("UPDATE game SET pushed_at = ? WHERE id = ?",
                         [(now, i) for i in ids])
