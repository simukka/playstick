"""The sources, and everything they have offered. Qt-free.

This is the Prepare tab's table, held rather than recomputed. The old tab
recomputed it: every rescan walked the library, diffed it against an index and
a state file, and drew the difference. That was affordable for one directory on
one mount and it is not affordable for six roots across three of them -- and it
also meant the tab had nothing to draw until a walk finished, which on a
sleeping NAS is the first thirty seconds of every launch.

Held, the tab opens instantly showing what it knew last time, and a scan
becomes a background correction to it rather than a precondition for it.

WHAT AN ITEM IS AND IS NOT

An item is a file some source offered, in whatever state. It is not a decision:
`available` means nobody has said anything about it, which is the state almost
everything a big collection contains will stay in forever. Only the rows
somebody selects become jobs, and only the ones that finish become films.

A file the scan no longer finds goes to `missing` rather than being deleted.
Deleting it would lose the only record that the thing was ever there -- and for
a film already in the library that record is what a removal needs in order to
take its artifacts back off the share.
"""

import json
import os
import time

# The item lifecycle. Deliberately strings in a column rather than an integer
# enum: this table is read with sqlite3 on the command line more often than it
# is read by this app, and "preparing" says what 3 does not.
AVAILABLE = "available"
QUEUED = "queued"
PREPARING = "preparing"
READY = "ready"
FAILED = "failed"
MISSING = "missing"
EXCLUDED = "excluded"

# The states that mean "somebody has asked for this and it has not finished".
# A rescan must not disturb these, and neither must a second Add to library.
IN_FLIGHT = (QUEUED, PREPARING)


# --- sources ---------------------------------------------------------------

def add_source(db, path, label=""):
    """Add a directory to scan. Returns its row id.

    Idempotent on the resolved path, so adding the same directory twice -- once
    typed, once picked through the file dialog with a trailing slash -- is one
    source and not two views of the same films.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    with db.write() as conn:
        conn.execute(
            "INSERT INTO source (path, label, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT (path) DO UPDATE SET enabled = 1",
            (resolved, label or os.path.basename(resolved.rstrip(os.sep)),
             time.time()))
    return db.one("SELECT id FROM source WHERE path = ?", (resolved,))["id"]


def sources(db, enabled_only=False):
    sql = "SELECT * FROM source"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return db.query(sql + " ORDER BY label, path")


def get_source(db, source_id):
    return db.one("SELECT * FROM source WHERE id = ?", (source_id,))


def set_source_enabled(db, source_id, enabled):
    with db.write() as conn:
        conn.execute("UPDATE source SET enabled = ? WHERE id = ?",
                     (1 if enabled else 0, source_id))


def remove_source(db, source_id):
    """Stop scanning a directory.

    Its items survive with a null source_id -- see the comment on that column.
    Removing a source is not a statement about films that are already on the
    NAS and on the stick, and a cascade would have made it one.
    """
    with db.write() as conn:
        conn.execute("DELETE FROM source WHERE id = ?", (source_id,))


# Which source is the share, by the path it was added under: when Settings
# names another share, the old one's source is the one to take away.
SHARE_META = "share_source"


def ensure_share_source(db, nas_root):
    """Make the share itself a source, and the previous share not one.

    The share is where films end up, and very often where they already are --
    a film that needs no encode is recorded where it lies and copies nothing --
    so it is offered without anybody having to add it. Returns (source id or
    None, whether it was added just now), so the caller knows to scan it.
    """
    resolved = (os.path.realpath(os.path.expanduser(nas_root))
                if nas_root else "")
    previous = db.get_meta(SHARE_META) or ""
    if previous and previous != resolved:
        row = db.one("SELECT id FROM source WHERE path = ?", (previous,))
        if row is not None:
            remove_source(db, row["id"])
    db.set_meta(SHARE_META, resolved)
    if not resolved:
        return None, False
    existed = db.one("SELECT id FROM source WHERE path = ?",
                     (resolved,)) is not None
    return add_source(db, resolved, "NAS"), not existed


def share_source_path(db):
    return db.get_meta(SHARE_META) or ""


def touch_scanned(db, source_id, when=None):
    with db.write() as conn:
        conn.execute("UPDATE source SET scanned_at = ? WHERE id = ?",
                     (when or time.time(), source_id))


# --- items -----------------------------------------------------------------

def upsert_item(db, ident, source_id, rel, abs_path, nas_rel=None, size=0,
                mtime=None, title="", year=None, state=None,
                excluded_reason=None, conn=None):
    """Record one file a scan found. Returns nothing; the id is the caller's.

    The id is the identity, not (source_id, rel). A file reachable through two
    sources -- somebody adds the NAS itself as well as the drive it copies from
    -- is one film with one id, and the source that found it first keeps it.
    Letting the second source claim it would make the row flap between two
    parents on every scan, and every flap is a redraw of a table somebody is
    reading.

    `state` is only ever written on INSERT. A rescan must not walk a queued film
    back to available, which is precisely what an unconditional update would do
    to anything the user selected between one scan and the next.
    """
    own = conn is None
    conn = conn or db.write()
    now = time.time()
    sql = (
        "INSERT INTO item (id, source_id, rel, abs_path, on_nas, nas_rel, "
        "size, mtime, title, year, state, excluded_reason, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  abs_path = excluded.abs_path, size = excluded.size, "
        "  mtime = excluded.mtime, seen_at = excluded.seen_at, "
        # A file that came back is not missing any more, and a file discovery
        # used to reject and now accepts -- an episode, before episodes were
        # offered -- is not excluded any more. Everything else the user or the
        # queue put there is left exactly as it was.
        "  state = CASE WHEN item.state = 'missing' THEN 'available' "
        "               WHEN item.state = 'excluded' "
        "                    AND excluded.state <> 'excluded' "
        "               THEN 'available' "
        "               ELSE item.state END, "
        "  title = CASE WHEN item.state = 'excluded' "
        "                    AND excluded.state <> 'excluded' "
        "               THEN excluded.title ELSE item.title END, "
        "  excluded_reason = CASE WHEN excluded.state = 'excluded' "
        "               THEN item.excluded_reason ELSE NULL END")
    params = (ident, source_id, rel, abs_path, 1 if nas_rel else 0, nas_rel,
              int(size or 0), mtime, title, year, state or AVAILABLE,
              excluded_reason, now)
    if own:
        with conn:
            conn.execute(sql, params)
    else:
        conn.execute(sql, params)


def mark_unseen_missing(db, source_id, before):
    """Everything this source offered before the scan and did not offer during
    it. Returns how many.

    Not a delete: a film already in the library keeps its row, because removing
    it from the library later needs to know what its artifacts on the share are
    called, and a deleted row is a set of files nothing can name.
    """
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE item SET state = ? "
            "WHERE source_id = ? AND seen_at < ? AND state NOT IN (?, ?, ?)",
            (MISSING, source_id, before, MISSING, QUEUED, PREPARING))
    return cur.rowcount


def get_item(db, ident):
    return db.one("SELECT * FROM item WHERE id = ?", (ident,))


def items(db, source_id=None, states=None):
    """The table, joined to what the source is called and whether it is in the
    library, which is what every caller wants and what two queries would cost a
    redraw."""
    sql = ("SELECT item.*, source.label AS source_label, "
           "       source.path AS source_path, "
           "       prepared.pushed_at AS pushed_at, "
           "       prepared.item_id IS NOT NULL AS in_library "
           "FROM item "
           "LEFT JOIN source ON source.id = item.source_id "
           "LEFT JOIN prepared ON prepared.item_id = item.id")
    where, params = [], []
    if source_id is not None:
        where.append("item.source_id = ?")
        params.append(source_id)
    if states:
        where.append("item.state IN (%s)" % ",".join("?" * len(states)))
        params.extend(states)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return db.query(sql + " ORDER BY item.title, item.rel", tuple(params))


def get_joined(db, ident):
    """One item with its source's path attached.

    A preparation needs both -- the file, and the root it was found under, which
    is what the preparer reads a year and a season folder out of -- and a row
    without the second is a row that cannot be prepared. One query rather than
    two so there is no window in which the source has been removed between them.
    """
    return db.one(
        "SELECT item.*, source.path AS source_path, source.label AS source_label "
        "FROM item LEFT JOIN source ON source.id = item.source_id "
        "WHERE item.id = ?", (ident,))


def set_state(db, ident, state, excluded_reason=None):
    with db.write() as conn:
        conn.execute(
            "UPDATE item SET state = ?, excluded_reason = ? WHERE id = ?",
            (state, excluded_reason, ident))


def source_counts(db):
    """{source id: films it offers}, for Config's Sources tab. A film a scan
    no longer finds is not counted: it is not something anybody can choose."""
    return {row["source_id"]: row["n"] for row in db.query(
        "SELECT source_id, COUNT(*) AS n FROM item "
        "WHERE state != 'missing' AND source_id IS NOT NULL "
        "GROUP BY source_id")}


def counts(db):
    """{state: n} for the tab's summary line."""
    return {row["state"]: row["n"] for row in
            db.query("SELECT state, COUNT(*) AS n FROM item GROUP BY state")}


# --- the probe cache -------------------------------------------------------
#
# This is .playstick/prep-state.json, off the share and one row per film. The
# preparer keys its cache by a path and guards it with a fingerprint; both come
# through unchanged, because the reader is still playstick_prep.collect and it
# has to recognise what it wrote.

def fingerprint(size, mtime, verify_mode, meta_version):
    return "%d:%d:%s:%d" % (int(size or 0), int(mtime or 0), verify_mode,
                            meta_version)


def save_probe(db, ident, movie, fp, conn=None):
    """Keep what one ffprobe learned, so the next run does not pay for it again.

    `movie` is the preparer's own dict minus "source", which is an absolute
    path on this host and therefore the one key in it that is not worth keeping.
    """
    body = json.dumps({k: v for k, v in movie.items() if k != "source"})
    sql = ("UPDATE item SET probe_json = ?, probe_fingerprint = ?, "
           "  duration = ?, width = ?, height = ?, vcodec = ?, acodec = ?, "
           "  verified = ?, title = ?, year = ? WHERE id = ?")
    params = (body, fp, movie.get("duration"), movie.get("width"),
              movie.get("height"), movie.get("vcodec"), movie.get("acodec"),
              movie.get("verified"), movie.get("title") or "",
              movie.get("year"), ident)
    if conn is None:
        with db.write() as owned:
            owned.execute(sql, params)
    else:
        conn.execute(sql, params)


def load_probe(db, ident, fp):
    """The cached probe, but only if it was taken under the same rules.

    A mismatched fingerprint returns nothing rather than something stale. The
    preparer makes the same check for the same reason: the rules that turn a
    file into a title and a year live in that file, not in the library, so a fix
    to them reaches a prepared library only by re-deriving what was cached under
    the old ones.
    """
    row = db.one(
        "SELECT probe_json FROM item WHERE id = ? AND probe_fingerprint = ?",
        (ident, fp))
    if row is None or not row["probe_json"]:
        return None
    try:
        return json.loads(row["probe_json"])
    except ValueError:
        return None
