"""The movie table: what the device holds, and what to call it.

Everything a client can address comes from here. The opaque ids the HTTP layer
accepts are the keys of this table, and Library._under_root is what proves a
path stored in a row actually sits under the library root before any other code
is allowed to open it.

WHERE THE TABLE COMES FROM, WHICH CHANGED

It used to come from somewhere else: a JSON index the preparer wrote onto the
share, re-read every five minutes, with a recursive walk of that share as the
fallback when the index was missing or unreadable. Both are gone. The films are
rows in the database now, put there by the admin app over /api/admin/films, and
this class reads them.

The consequence worth stating plainly: a device whose database is empty shows
an empty library, and no amount of waiting fills it. That is honest -- it says
"nobody has synced me" rather than spending four minutes stat-ing a share over
SDIO Wi-Fi to rediscover what another machine already knew. The sync is one
button in the admin app.

WHAT THE BACKGROUND THREAD STILL DOES

One stat, on SCAN_INTERVAL, asking whether the share is reachable. That is all
`available` ever meant to the page -- it is what turns the grid into "the films
are unreachable" and offers the button that switches the NAS back on -- and it
no longer costs a walk to answer.
"""

import json
import os
import threading
import time

from .config import LIBRARY, SCAN_INTERVAL, log


# What the desktop admin editor is allowed to set. Everything a film carries
# that is NOT in here is a filesystem path this process resolved and proved
# sits under the library root -- source_rel, media_rel, and the per-track rels
# inside subtitles and audio. Letting one of those into this set would mean a
# metadata edit could substitute a path, which is a different endpoint with
# different rules; /api/admin/films is where paths are set, and it validates
# every one of them. An edit naming a path here is dropped rather than obeyed.
#
# The rest -- what the film is called, when it is from, how it scores, what
# kinds it is, and whether the children see it at all -- is metadata, and
# metadata is exactly what the person at the desktop is there to fix.
EDITABLE = frozenset((
    "title", "sort_title", "year", "rating", "genres", "hidden",
))

# The columns on `film` a sync may set, in the order the INSERT below wants
# them. Deliberately explicit rather than derived from the row dict: a payload
# arriving over HTTP must not be able to decide which columns it writes.
SYNC_COLUMNS = (
    "id", "source_rel", "media_rel", "title", "sort_title", "year", "rating",
    "plot", "duration", "width", "height", "vcodec", "acodec", "tmdb_id",
    "hidden", "pos",
)


class Library:
    """The film table, read from the database and merged with its overrides.

    No cache and no lock, which is the difference the database makes. The old
    class held every film in a dict behind a mutex because the alternative was
    a rescan over CIFS; a SELECT against a local WAL database is cheap enough
    that /api/library can simply ask, and asking means an edit made on one
    thread is visible to the next request without any invalidation to get wrong.
    """

    def __init__(self, db):
        self._db = db
        self._wake = threading.Event()
        # The share's reachability, which is all the background thread now
        # decides. Guarded because a request thread reads it while the keeper
        # writes it, and a bare bool assignment being atomic in CPython is not
        # a thing worth resting on.
        self._lock = threading.Lock()
        self._available = False
        self._scanned_at = 0.0
        self._error = ""

    # -- what the HTTP layer asks for --------------------------------------

    def snapshot(self):
        """The whole table, in shelf order, with overrides applied.

        Returns the same five-tuple it always did -- (order, items, available,
        scanned_at, error) -- unpacked positionally by the handler.
        """
        items, order = self._read_all()
        with self._lock:
            return (order, items, self._available, self._scanned_at,
                    self._error)

    def get(self, ident):
        """One film, merged, or None. The same dict shape snapshot() yields."""
        rows = self._db.query("SELECT * FROM film WHERE id = ?", (ident,))
        if not rows:
            return None
        items = self._hydrate(rows)
        return items.get(ident)

    def available(self):
        with self._lock:
            return self._available

    # -- reading ------------------------------------------------------------

    def _read_all(self):
        rows = self._db.query(
            "SELECT * FROM film ORDER BY pos, sort_title, title")
        items = self._hydrate(rows)
        # The database's order, not a re-sort: the preparer files by a
        # normalised title, so "The Fifth Element" sits under F the way it
        # would on a shelf, which a plain sort on `title` cannot do. Films the
        # join dropped -- a media path that no longer resolves under the root
        # -- fall out of the order with it.
        order = [r["id"] for r in rows if r["id"] in items]
        return items, order

    def _hydrate(self, rows):
        """Rows plus their children plus their overrides, as the item dicts the
        rest of the daemon has always been handed.

        One query per child table for the whole batch rather than per film: a
        library of three hundred films is four queries here and was three
        hundred round trips over CIFS before.
        """
        if not rows:
            return {}
        root = os.path.realpath(LIBRARY)
        ids = [r["id"] for r in rows]
        genres = self._grouped(
            "SELECT film_id, genre FROM film_genre WHERE film_id IN (%s) "
            "ORDER BY pos" % self._marks(ids), ids)
        subs = self._grouped(
            "SELECT film_id, rel, lang FROM subtitle WHERE film_id IN (%s) "
            "ORDER BY pos" % self._marks(ids), ids)
        audio = self._grouped(
            "SELECT film_id, n, rel, lang, title, channels, is_default, "
            "offset FROM audio_track WHERE film_id IN (%s) ORDER BY n"
            % self._marks(ids), ids)
        overrides = self._grouped(
            "SELECT film_id, field, value FROM override WHERE film_id IN (%s)"
            % self._marks(ids), ids)

        items = {}
        for row in rows:
            ident = row["id"]
            media = self._under_root(root, row["media_rel"])
            if media is None:
                # A row whose path does not resolve inside the library is a row
                # that must not be playable, and dropping it here is the same
                # judgement the index reader made: one bad entry should not
                # cost the other ninety-nine. It is logged rather than silent
                # because it can only come from a sync that got past the
                # validation on the way in, which would be a bug worth seeing.
                log("film %s names %r, which is not under the library root; "
                    "skipping", ident, row["media_rel"])
                continue
            item = {
                "id": ident,
                "title": row["title"],
                "path": media,
                # Kept in the shape the rest of the daemon reads even though
                # nothing sets it any more: sidecar posters were something the
                # walk found on the share, and the walk is gone. Thumbs still
                # asks for it, and answering None is cheaper than teaching two
                # call sites that the key may be absent.
                "sidecar": None,
                # Likewise: a poster is a blob in the database now, not a file,
                # so there is no path to carry. /api/thumb asks Thumbs.
                "poster": None,
                # Path AND language. The language is what the phone's subtitle
                # sheet writes on the row somebody taps, and mpv is not a
                # reliable source for it: an --sub-file it was handed
                # explicitly carries whatever mpv managed to guess from the
                # filename, where the database knows what prep actually
                # extracted.
                #
                # Stat'ed, unlike audio below, because these are handed to mpv
                # on the command line and one that is not there is a hard error
                # at film start.
                "subs": [s for s in ({"path": self._under_root(root, r["rel"]),
                                      "lang": (r["lang"] or "und").lower()}
                                     for r in subs.get(ident, ()))
                         if s["path"] and os.path.isfile(s["path"])],
                # Not stat'ed, deliberately, where "subs" just above is.
                #
                # The difference is what a missing file costs. An audio track
                # that has gone missing is a 404 on a request nobody is blocked
                # by, and the page has to be able to explain that case anyway.
                # Stat'ing them would mean up to four more round trips per film
                # over CIFS on every library read, which is the exact cost the
                # database exists to avoid.
                "audio": [t for t in (
                    {"n": r["n"],
                     "path": self._under_root(root, r["rel"]),
                     "lang": r["lang"] or "und",
                     "title": r["title"] or "",
                     "channels": r["channels"],
                     "default": bool(r["is_default"]),
                     "offset": float(r["offset"] or 0.0)}
                    for r in audio.get(ident, ())
                ) if t["path"]],
                "year": row["year"],
                "rating": _rating(row["rating"]),
                "genres": [r["genre"] for r in genres.get(ident, ())],
                # Prep's shelf key: "The Fifth Element" sorts under F. Carried
                # so the page can offer a name sort that agrees with the order
                # the rows already arrive in, rather than one that files every
                # "The" together.
                "sort_title": row["sort_title"] or "",
                "hidden": bool(row["hidden"]),
                "plot": row["plot"] or "",
                "duration": row["duration"],
            }
            # The overlay, laid on top. A plain update, which is why EDITABLE
            # keeps paths out of the override table: this is the join, and it
            # cannot tell a corrected title from a substituted path.
            for over in overrides.get(ident, ()):
                if over["field"] in EDITABLE:
                    item[over["field"]] = json.loads(over["value"])
            item["hidden"] = bool(item["hidden"])
            items[ident] = item
        return items

    @staticmethod
    def _marks(ids):
        return ",".join("?" * len(ids))

    def _grouped(self, sql, ids):
        out = {}
        for row in self._db.query(sql, tuple(ids)):
            out.setdefault(row["film_id"], []).append(row)
        return out

    @staticmethod
    def _under_root(root, rel):
        """A library-relative path from a row, resolved and proved to be inside
        the library.

        The rows are written by another machine over HTTP and their paths are
        attacker-shaped input as far as this process is concerned -- the same
        check start() makes before handing anything to mpv, and the same one
        the sync route makes before storing a row at all. Two checks rather
        than one on purpose: the route refuses to store a path that escapes,
        and this refuses to use one, so neither a bug in the first nor a
        database edited by hand can put a path outside the share in front of
        mpv.
        """
        if not rel:
            return None
        candidate = os.path.realpath(os.path.join(root, rel))
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
        return None

    # -- editing ------------------------------------------------------------

    def apply_override(self, ident, fields):
        """Record one desktop edit and return the film as it now reads.

        `fields` is already coerced by the HTTP layer to the shape each key
        wants; a value of None means "forget this edit and go back to what the
        sync said", which is how the editor's per-field reset works -- and why
        the overrides are their own table rather than columns on `film`.

        Anything outside EDITABLE is ignored here as a second line after the
        handler's filtering: the stored row must never carry a path.

        Returns the merged film dict, or None when the id is not one we have.
        """
        clean = {k: v for k, v in fields.items() if k in EDITABLE}
        if not self._db.one("SELECT 1 FROM film WHERE id = ?", (ident,)):
            return None
        conn = self._db.write()
        with conn:
            for key, value in clean.items():
                if value is None:
                    conn.execute(
                        "DELETE FROM override WHERE film_id = ? AND field = ?",
                        (ident, key))
                else:
                    conn.execute(
                        "INSERT INTO override (film_id, field, value) "
                        "VALUES (?, ?, ?) ON CONFLICT (film_id, field) "
                        "DO UPDATE SET value = excluded.value",
                        (ident, key, json.dumps(value)))
        return self.get(ident)

    def overrides(self):
        """Every edit, as {film_id: {field: value}}.

        The admin app reads this back to write the preparer's overrides file,
        which is how a title corrected on the device survives the next prep run
        -- there is one home for an edit now, and this is the door out of it.
        """
        out = {}
        for row in self._db.query("SELECT film_id, field, value FROM override"):
            if row["field"] in EDITABLE:
                out.setdefault(row["film_id"], {})[row["field"]] = \
                    json.loads(row["value"])
        return out

    # -- syncing ------------------------------------------------------------

    def sync(self, films, reconcile=False):
        """Replace the film table with what the admin app just sent.

        Upsert, never delete-and-reinsert, and that is not a micro-optimisation
        -- `playback` cascades from `film`, so a sync that deleted every row
        would throw away every resume position in the house each time somebody
        pressed the button. Films absent from the batch are deleted only when
        `reconcile` is set, which is what a full library push asks for and what
        a single corrected film does not.

        Every path in `films` has already been validated by the HTTP layer.
        The whole thing is one transaction: a sync that fails halfway leaves
        the library as it was, rather than half of a new one.

        Returns (written, removed).
        """
        now = time.time()
        conn = self._db.write()
        with conn:
            for pos, film in enumerate(films):
                self._upsert(conn, film, pos, now)
            removed = 0
            if reconcile:
                keep = [f["id"] for f in films]
                if keep:
                    cur = conn.execute(
                        "DELETE FROM film WHERE id NOT IN (%s)"
                        % self._marks(keep), tuple(keep))
                else:
                    cur = conn.execute("DELETE FROM film")
                removed = cur.rowcount
            self._renumber(conn)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('synced_at', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (repr(now),))
        return len(films), removed

    @staticmethod
    def _renumber(conn):
        """Put the whole shelf back in order, however this sync arrived.

        `pos` used to be the film's index in the batch, which was right while
        the only kind of sync was a whole library at once -- the admin app sent
        every film in shelf order and the numbering fell out of the loop.

        It stopped being right the moment films started arriving ONE AT A TIME,
        which is what the admin app does now: each finished preparation is a
        push of a single film with reconcile off. That film's index in its own
        batch is zero, so it took position zero on the grid. Every film added
        would have sat at the top, in reverse order of when it was added, and
        nothing anywhere would have reported a problem -- the sync succeeds, the
        poster appears, and the shelf is simply wrong.

        So the order is derived here instead, from `sort_title`, which is what
        `pos` was carrying all along: the preparer files a film under a
        normalised title so that "The Fifth Element" sits under F the way it
        would on a shelf. A correlated subquery rather than a window function
        because it is a few hundred rows and this box has four 1.44 GHz cores
        and an sqlite of whatever vintage the image shipped with.
        """
        conn.execute(
            "UPDATE film SET pos = ("
            "  SELECT COUNT(*) FROM film AS other"
            "   WHERE other.sort_title < film.sort_title"
            "      OR (other.sort_title = film.sort_title"
            "          AND other.title < film.title)"
            "      OR (other.sort_title = film.sort_title"
            "          AND other.title = film.title AND other.id < film.id))")

    def _upsert(self, conn, film, pos, now):
        values = [film.get(c) for c in SYNC_COLUMNS]
        values[SYNC_COLUMNS.index("pos")] = pos
        conn.execute(
            "INSERT INTO film (%s, updated_at) VALUES (%s, ?) "
            "ON CONFLICT (id) DO UPDATE SET %s, updated_at = excluded.updated_at"
            % (", ".join(SYNC_COLUMNS),
               ",".join("?" * len(SYNC_COLUMNS)),
               ", ".join("%s = excluded.%s" % (c, c)
                         for c in SYNC_COLUMNS if c != "id")),
            tuple(values) + (now,))
        ident = film["id"]
        # The children are replaced wholesale rather than reconciled row by
        # row. They are small, they have no identity of their own worth
        # preserving, and a film that lost a subtitle track must not keep it.
        # `thumb`, `override` and `playback` are deliberately NOT touched here:
        # those are the device's own, and a re-sync is not a reason to discard
        # a poster somebody uploaded or a title somebody fixed.
        conn.execute("DELETE FROM film_genre WHERE film_id = ?", (ident,))
        conn.executemany(
            "INSERT OR IGNORE INTO film_genre (film_id, genre, pos) "
            "VALUES (?, ?, ?)",
            [(ident, g, i) for i, g in enumerate(film.get("genres") or ())])
        conn.execute("DELETE FROM subtitle WHERE film_id = ?", (ident,))
        conn.executemany(
            "INSERT OR IGNORE INTO subtitle (film_id, rel, lang, pos) "
            "VALUES (?, ?, ?, ?)",
            [(ident, s["rel"], s.get("lang") or "und", i)
             for i, s in enumerate(film.get("subtitles") or ())])
        conn.execute("DELETE FROM audio_track WHERE film_id = ?", (ident,))
        conn.executemany(
            "INSERT OR IGNORE INTO audio_track (film_id, n, rel, lang, title, "
            "channels, is_default, offset) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(ident, t["n"], t["rel"], t.get("lang") or "und",
              t.get("title") or "", t.get("channels"),
              1 if t.get("default") else 0, float(t.get("offset") or 0.0))
             for t in film.get("audio") or ()])

    def delete(self, ids):
        """Remove named films and everything that hangs off them.

        `thumb`, `override`, `playback`, `film_genre`, `subtitle` and
        `audio_track` all cascade, which is the point: a film that is being
        taken out of the library should not leave its resume position behind to
        be inherited by whatever is next given the same id.

        That is the opposite of what a re-sync must do, and the difference is
        worth being clear about. A sync is an upsert precisely so that pushing
        a film again does not discard the poster somebody uploaded or the title
        somebody fixed. This is somebody saying "take it away", which is the one
        case where discarding all of it is the whole request.
        """
        if not ids:
            return 0
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM film WHERE id IN (%s)"
                               % self._marks(ids), tuple(ids))
            removed = cur.rowcount
            if removed:
                self._renumber(conn)
        return removed

    def rows(self):
        """Every film as it is stored, paths and all, for the admin app.

        The one read that does NOT go through _under_root or apply overrides:
        the admin app is diffing against what it sent, so it needs the row, not
        the resolved and merged view the phones get. It is also the only place
        a stored path leaves this process, which is why it is a separate method
        rather than a flag on snapshot().
        """
        out = []
        rows = self._db.query("SELECT * FROM film ORDER BY pos, sort_title")
        ids = [r["id"] for r in rows]
        if not ids:
            return out
        genres = self._grouped(
            "SELECT film_id, genre FROM film_genre WHERE film_id IN (%s) "
            "ORDER BY pos" % self._marks(ids), ids)
        overrides = self.overrides()
        thumbs = {r["film_id"]: r for r in self._db.query(
            "SELECT film_id, etag, source FROM thumb WHERE film_id IN (%s)"
            % self._marks(ids), ids)}
        for row in rows:
            ident = row["id"]
            thumb = thumbs.get(ident)
            out.append({
                "id": ident,
                "source_rel": row["source_rel"],
                "media_rel": row["media_rel"],
                "title": row["title"],
                "sort_title": row["sort_title"] or "",
                "year": row["year"],
                "rating": _rating(row["rating"]),
                "plot": row["plot"] or "",
                "duration": row["duration"],
                "tmdb_id": row["tmdb_id"],
                "hidden": bool(row["hidden"]),
                "genres": [r["genre"] for r in genres.get(ident, ())],
                "overrides": overrides.get(ident, {}),
                "thumb_etag": thumb["etag"] if thumb else "",
                "thumb_source": thumb["source"] if thumb else "",
            })
        return out

    # -- the share ----------------------------------------------------------

    def request_rescan(self):
        self._wake.set()

    def run(self, stopping):
        while not stopping.is_set():
            self._probe()
            self._wake.wait(SCAN_INTERVAL)
            self._wake.clear()

    def _probe(self):
        """Is the share there? One stat, where this used to be a whole walk.

        os.path.isdir on an automount trigger is what causes the mount to
        happen, which is the behaviour wanted: the point of asking is to find
        out whether a film could be played right now.
        """
        error = ""
        try:
            available = os.path.isdir(LIBRARY)
            if not available:
                error = "the share is not mounted"
        except OSError as exc:
            available = False
            error = os.strerror(exc.errno) if exc.errno else str(exc)
        if not available and self.available():
            log("library unavailable: %s", error)
        with self._lock:
            self._available = available
            self._scanned_at = time.time()
            self._error = error


def _rating(value):
    """A rating back out of its TEXT column as whatever it went in as.

    Stored as text because it is both -- a score like 7.8 from TMDb, or a
    certificate like "PG" from a .nfo -- and a column cannot be both. The page
    formats a number differently from a word, so the distinction has to survive
    the round trip; doing it here rather than in a REAL column plus a TEXT one
    keeps the two spellings of "how this film scores" in one place.
    """
    if value is None or value == "":
        return None
    try:
        return float(value) if "." in value else int(value)
    except (TypeError, ValueError):
        return value
