"""What has been made, what is on the share, and what somebody corrected.

Three tables and one idea: this is the half of a film's record that the stick
does not own. `prepared` and its children are what a push to the device is
built from -- they replace playstick-library.json, which used to sit on the
share being read by nothing on the share. `poster` holds the bytes, because the
stick keeps posters in its own database and has never opened one off the mount.
`override` holds corrections, because the preparer has to honour them on the
next run and the file that used to hold them lived on a read-only mount.

WHY `override` IS ONE ROW PER FIELD

The same reason the daemon's table of the same name is. The preparer derives a
title from a filename; this holds what somebody typed instead. Kept apart, and
kept per field, so that clearing a correction drops back to what the file
actually says rather than to the last value anybody entered -- a reset is a
DELETE here, not a write of the old value. A film with no rows here reads
exactly as it was prepared.

The device has the same table and it is not a duplicate of this one. An edit
made from the sofa lands there; sync.pull_overrides copies it here, so that the
next preparation honours it instead of deriving the old answer from the
filename again and pushing it back over the top. That round trip is the whole
reason both exist.

THE FIELD LISTS ARE A CONTRACT WITH TWO OTHER FILES

playstick_prep.apply_overrides reads the shape SCALAR_FIELDS describes, and the
daemon accepts exactly SCALAR_FIELDS + FLAG_FIELDS minus `plot` on
/api/admin/item. META_FIELDS are this app's own and the device has no column
for any of them -- a pinned TMDb match in particular is a decision somebody made
here, and a pull from a device that has never heard of it must not clear it.
"""

import hashlib
import json
import time

SCALAR_FIELDS = ("title", "sort_title", "year", "rating", "genres", "plot")
FLAG_FIELDS = ("hidden",)
META_FIELDS = ("tmdb_id", "note")


# --- the prepared film -----------------------------------------------------

def record(db, ident, movie, media_rel, source_rel, bytes_on_nas=0,
           poster=None):
    """Everything a finished job learned, in one transaction.

    One commit and not several, for the reason the daemon gives about a sync:
    a half-written record is a film that is on the share, unknown to this app,
    and invisible to the orphan sweep that would otherwise reclaim it. Either
    the film is prepared or it is not.

    `poster` is (bytes, mime, source) or None. It is stored here rather than
    moved to the share deliberately -- see the schema.
    """
    now = time.time()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO prepared (item_id, media_rel, source_rel, title, "
            "  sort_title, year, rating, plot, duration, width, height, "
            "  vcodec, acodec, tmdb_id, tmdb_score, metadata_source, "
            "  poster_source, hidden, notes, bytes_on_nas, prepared_at, "
            "  series, season, episode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (item_id) DO UPDATE SET "
            + ", ".join("%s = excluded.%s" % (c, c) for c in (
                "media_rel", "source_rel", "title", "sort_title", "year",
                "rating", "plot", "duration", "width", "height", "vcodec",
                "acodec", "tmdb_id", "tmdb_score", "metadata_source",
                "poster_source", "hidden", "notes", "bytes_on_nas",
                "prepared_at", "series", "season", "episode"))
            # A re-prepare has not been pushed yet, whatever the old row said.
            + ", pushed_at = NULL",
            (ident, media_rel, source_rel, movie.get("title") or ident,
             movie.get("sort_title") or "", movie.get("year"),
             _text(movie.get("rating")), movie.get("plot"),
             movie.get("duration"), movie.get("width"), movie.get("height"),
             movie.get("vcodec"), movie.get("acodec"), movie.get("tmdb_id"),
             movie.get("tmdb_score"), movie.get("metadata_source"),
             movie.get("poster_source"), 1 if movie.get("hidden") else 0,
             json.dumps(movie.get("verify_notes") or []), int(bytes_on_nas),
             now, movie.get("series") or None, movie.get("season"),
             movie.get("episode")))

        # The children are replaced wholesale rather than reconciled row by
        # row -- they are small, they have no identity worth preserving, and a
        # re-prepare that dropped a subtitle track must not leave it behind.
        # The same judgement Library._upsert makes on the device.
        conn.execute("DELETE FROM prepared_genre WHERE item_id = ?", (ident,))
        conn.executemany(
            "INSERT OR IGNORE INTO prepared_genre (item_id, genre, pos) "
            "VALUES (?, ?, ?)",
            [(ident, g, i) for i, g in enumerate(movie.get("genres") or ())])
        conn.execute("DELETE FROM prepared_subtitle WHERE item_id = ?", (ident,))
        conn.executemany(
            "INSERT OR IGNORE INTO prepared_subtitle (item_id, rel, lang, pos) "
            "VALUES (?, ?, ?, ?)",
            [(ident, s["rel"].replace("\\", "/"), s.get("lang") or "und", i)
             for i, s in enumerate(movie.get("subtitles") or ())])
        conn.execute("DELETE FROM prepared_audio WHERE item_id = ?", (ident,))
        conn.executemany(
            "INSERT OR IGNORE INTO prepared_audio (item_id, n, rel, lang, "
            "title, channels, is_default, offset) VALUES (?,?,?,?,?,?,?,?)",
            [(ident, t["n"], t["rel"].replace("\\", "/"),
              t.get("lang") or "und", t.get("title") or "", t.get("channels"),
              1 if t.get("default") else 0, float(t.get("offset") or 0.0))
             for t in movie.get("phone_audio") or ()])

        if poster is not None:
            data, mime, source = poster
            conn.execute(
                "INSERT INTO poster (item_id, mime, bytes, etag, source, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (item_id) DO UPDATE SET mime = excluded.mime, "
                "bytes = excluded.bytes, etag = excluded.etag, "
                "source = excluded.source, updated_at = excluded.updated_at",
                (ident, mime, data, etag_of(data), source or "", now))


def _text(value):
    """`rating` is text on both databases because it is both 7.8 and "PG".
    Coerced here rather than at the column, which would be a type the data has
    not got."""
    return None if value in (None, "") else str(value)


def get(db, ident):
    return db.one("SELECT * FROM prepared WHERE item_id = ?", (ident,))


def all_prepared(db):
    return db.query("SELECT * FROM prepared ORDER BY sort_title, title")


def children(db, ident):
    """The genres, subtitles and audio tracks of one film, in the shapes the
    device payload wants them."""
    return {
        "genres": [r["genre"] for r in db.query(
            "SELECT genre FROM prepared_genre WHERE item_id = ? ORDER BY pos",
            (ident,))],
        "subtitles": [dict(r) for r in db.query(
            "SELECT rel, lang FROM prepared_subtitle WHERE item_id = ? "
            "ORDER BY pos", (ident,))],
        "audio": [dict(r) for r in db.query(
            "SELECT n, rel, lang, title, channels, is_default, offset "
            "FROM prepared_audio WHERE item_id = ? ORDER BY n", (ident,))],
    }


def artifact_rels(db, ident):
    """Every file this film owns on the share.

    What a removal deletes and what the orphan sweep compares against. The
    poster is deliberately not in it: it never went to the share.
    """
    row = get(db, ident)
    if row is None:
        return []
    rels = {row["media_rel"], row["source_rel"]}
    rels |= {r["rel"] for r in db.query(
        "SELECT rel FROM prepared_subtitle WHERE item_id = ?", (ident,))}
    rels |= {r["rel"] for r in db.query(
        "SELECT rel FROM prepared_audio WHERE item_id = ?", (ident,))}
    # A film that was already on the share plays from where it lies, and its
    # own file is not this app's to delete -- only what preparing it created.
    return sorted(r for r in rels if r and r.startswith("."))


def forget(db, ident):
    """Drop the record of a prepared film. The files are nas_store's business
    and the device row is sync's; this is only the bookkeeping."""
    with db.write() as conn:
        conn.execute("DELETE FROM prepared WHERE item_id = ?", (ident,))
        conn.execute("DELETE FROM poster WHERE item_id = ?", (ident,))


def mark_pushed(db, ident, when=None):
    with db.write() as conn:
        conn.execute("UPDATE prepared SET pushed_at = ? WHERE item_id = ?",
                     (when or time.time(), ident))


def unpushed(db):
    """Films that are on the share and that the stick has not been told about.

    Not an error state: a queue that finished while the stick was switched off
    leaves exactly this, and the fix is one button rather than preparing
    anything again.
    """
    return db.query(
        "SELECT * FROM prepared WHERE pushed_at IS NULL ORDER BY sort_title")


# --- posters ---------------------------------------------------------------

def etag_of(data):
    """The identity the DEVICE computes for these bytes.

    Must stay in step with thumbs.etag_for() on the daemon: the two are compared
    across a network, and a difference in how they are derived means every
    poster uploaded on every sync, forever, with nothing reporting anything
    wrong.
    """
    return hashlib.sha1(data).hexdigest()[:16]


def poster(db, ident):
    return db.one("SELECT * FROM poster WHERE item_id = ?", (ident,))


def set_poster(db, ident, data, mime="image/jpeg", source="admin"):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO poster (item_id, mime, bytes, etag, source, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (item_id) DO UPDATE SET mime = excluded.mime, "
            "bytes = excluded.bytes, etag = excluded.etag, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (ident, mime, data, etag_of(data), source, time.time()))


# --- overrides -------------------------------------------------------------

def overrides(db, ident=None):
    """{item_id: {field: value}}, or one film's entry when `ident` is given.

    The shape playstick_prep.apply_overrides reads, so it can be handed straight
    to a preparation without translation.
    """
    if ident is None:
        rows = db.query("SELECT item_id, field, value FROM override")
    else:
        rows = db.query(
            "SELECT item_id, field, value FROM override WHERE item_id = ?",
            (ident,))
    out = {}
    for row in rows:
        try:
            value = json.loads(row["value"])
        except ValueError:
            continue
        out.setdefault(row["item_id"], {})[row["field"]] = value
    return out if ident is None else out.get(ident, {})


def set_override(db, ident, **fields):
    """Merge corrections into one film. A field passed as None -- or as "" or
    [] -- is a reset, and a reset is a DELETE: it drops back to what the file
    says rather than to the last value anybody typed."""
    with db.write() as conn:
        for key, value in fields.items():
            if value in (None, "", []):
                conn.execute(
                    "DELETE FROM override WHERE item_id = ? AND field = ?",
                    (ident, key))
            else:
                conn.execute(
                    "INSERT INTO override (item_id, field, value) "
                    "VALUES (?, ?, ?) ON CONFLICT (item_id, field) "
                    "DO UPDATE SET value = excluded.value",
                    (ident, key, json.dumps(value)))


def clear_overrides(db, ident):
    with db.write() as conn:
        conn.execute("DELETE FROM override WHERE item_id = ?", (ident,))
