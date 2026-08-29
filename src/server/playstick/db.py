"""The one file this appliance owns and writes: /var/lib/playstick/playstick.db.

WHY A DATABASE AND NOT THE FOUR JSON FILES IT REPLACES

The library used to arrive from somewhere else. A prep run on a developer
machine wrote playstick-library.json onto the share; this daemon re-read it
every five minutes, or walked the share itself when it could not. Edits went
into a second file beside it, the admin app wrote a third in a different
schema, and posters lived half on the share and half in /var/cache.

The share is mounted READ-ONLY -- see roles/nas/templates/nas.mount.j2 -- so
the edit file could never actually be written on the real device. Every title
corrected from the admin view was lost at the next restart, with one line in
the journal saying so. That is the bug this file closes: the device now holds
its own library, its own edits, its own posters and its own resume positions,
in one place it can write, on storage that is always there.

What stays outside is what SQLite has no business holding: the original film,
its re-encode, its subtitles and its per-language audio tracks. Those are files
on the share, addressed by a library-relative path in a row -- resolved through
Library._under_root before anything opens them, exactly as the index's paths
were.

ONE CONNECTION PER THREAD, DELIBERATELY

sqlite3 connections may not be shared across threads, and this daemon is a
ThreadingHTTPServer with four background workers besides. So each thread opens
its own on first use and keeps it. WAL is what makes that cheap: readers do not
block the writer and the writer does not block readers, which matters because
the writer here can be a poster extraction landing a 40 KB blob while six
phones poll /api/status.

The eMMC is the reason for synchronous=NORMAL rather than FULL. In WAL mode
NORMAL still cannot corrupt the database -- it risks losing the last commit or
two to a power cut, which for a resume position is the right trade on a device
somebody switches off at the wall.
"""

import os
import sqlite3
import threading

from .config import DB_PATH, log


# Bumped when SCHEMA below changes shape. The migration path is the list at the
# bottom of this file; a database at version N is brought forward by running
# every statement from N onwards. There is no downgrade, and there does not
# need to be: the whole store is rebuilt by one sync from the admin app.
SCHEMA_VERSION = 1


SCHEMA = """
-- The film table. `id` is sha1(source_rel)[:16] -- computed by the preparer
-- and never by this process, which is what lets a film keep its identity (and
-- therefore its poster and its resume position) across a re-prep.
--
-- Two paths, and the difference is load-bearing. source_rel is the original
-- file, kept so a re-sync can recognise the same film; media_rel is what mpv
-- is actually handed, which is the re-encode when the preparer made one. Both
-- are relative to the library root and neither is trusted until
-- Library._under_root has resolved it.
CREATE TABLE film (
  id          TEXT PRIMARY KEY,
  source_rel  TEXT NOT NULL,
  media_rel   TEXT NOT NULL,
  title       TEXT NOT NULL,
  sort_title  TEXT NOT NULL DEFAULT '',
  year        INTEGER,
  -- Whatever the .nfo held: a score like 7.8 or a certificate like "PG". Text
  -- because it is both, and the HTTP layer coerces a numeric-looking one back
  -- on the way out rather than this column pretending to a type it has not got.
  rating      TEXT,
  plot        TEXT,
  duration    REAL,
  width       INTEGER,
  height      INTEGER,
  vcodec      TEXT,
  acodec      TEXT,
  tmdb_id     INTEGER,
  hidden      INTEGER NOT NULL DEFAULT 0,
  -- The preparer's own shelf order is not preserved as a column: `pos` on the
  -- sync is what the daemon sorts by, so "The Fifth Element" sits under F the
  -- way it does on a shelf rather than under T.
  pos         INTEGER NOT NULL DEFAULT 0,
  updated_at  REAL NOT NULL
);
CREATE INDEX film_pos ON film (pos, sort_title);

-- A table rather than a JSON column, so that "every film tagged Fantasy" is a
-- query rather than a scan. `pos` keeps the preparer's order, which is TMDb's
-- order and puts the most characteristic genre first.
CREATE TABLE film_genre (
  film_id  TEXT NOT NULL REFERENCES film(id) ON DELETE CASCADE,
  genre    TEXT NOT NULL,
  pos      INTEGER NOT NULL,
  PRIMARY KEY (film_id, genre)
);

-- Extracted subtitles, as files. The language is carried because mpv is not a
-- reliable source for it -- an --sub-file it was handed explicitly reports
-- whatever it guessed from the filename, where the preparer knows what it
-- actually pulled out of the container.
CREATE TABLE subtitle (
  film_id  TEXT NOT NULL REFERENCES film(id) ON DELETE CASCADE,
  rel      TEXT NOT NULL,
  lang     TEXT NOT NULL DEFAULT 'und',
  pos      INTEGER NOT NULL,
  PRIMARY KEY (film_id, rel)
);

-- Per-language AAC for headphones. Files, not blobs: these are 50-150 MB each,
-- they are served over HTTP Range to a phone that reconnects on every seek,
-- and a byte range out of a file is a seek where a byte range out of a blob is
-- a read of the whole row.
CREATE TABLE audio_track (
  film_id     TEXT NOT NULL REFERENCES film(id) ON DELETE CASCADE,
  n           INTEGER NOT NULL,
  rel         TEXT NOT NULL,
  lang        TEXT NOT NULL DEFAULT 'und',
  title       TEXT NOT NULL DEFAULT '',
  channels    INTEGER,
  is_default  INTEGER NOT NULL DEFAULT 0,
  -- The extracted track's own origin against the film clock, measured by the
  -- preparer rather than assumed. Normally 0.0.
  offset      REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY (film_id, n)
);

-- Posters, as bytes. Small -- 400 px wide at JPEG q3, so tens of kilobytes --
-- and there is one per film, which is the shape a blob column is for.
-- `source` distinguishes a poster the admin app uploaded from a frame this
-- box extracted, so a re-sync can replace the first without discarding the
-- second. `etag` is sha1(bytes)[:16] and is what /api/thumb serves and
-- If-None-Match compares.
CREATE TABLE thumb (
  film_id     TEXT PRIMARY KEY REFERENCES film(id) ON DELETE CASCADE,
  mime        TEXT NOT NULL,
  bytes       BLOB NOT NULL,
  etag        TEXT NOT NULL,
  source      TEXT NOT NULL,
  updated_at  REAL NOT NULL
);

-- One row per EDITED FIELD, and that shape is the whole feature.
--
-- The film table holds what the preparer derived; this holds what somebody at
-- the admin view corrected. They are kept apart so that clearing an edited
-- field drops back to what the library actually says rather than to the last
-- value anybody typed -- a reset is a DELETE here, not a write of the old
-- value. A film with no row here reads exactly as it was prepared.
--
-- The value is JSON so that `genres` -- the one editable field that is a list
-- -- fits in the same column as the scalars.
CREATE TABLE override (
  film_id  TEXT NOT NULL REFERENCES film(id) ON DELETE CASCADE,
  field    TEXT NOT NULL,
  value    TEXT NOT NULL,
  PRIMARY KEY (film_id, field)
);

-- Where each film got to. Nothing wrote this before; it exists now because the
-- device finally has somewhere to put it.
CREATE TABLE playback (
  film_id     TEXT PRIMARY KEY REFERENCES film(id) ON DELETE CASCADE,
  position    REAL NOT NULL DEFAULT 0,
  duration    REAL,
  plays       INTEGER NOT NULL DEFAULT 0,
  finished    INTEGER NOT NULL DEFAULT 0,
  updated_at  REAL NOT NULL
);

CREATE TABLE meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);
"""


# Applied in order to bring a database forward. Index 0 creates everything from
# nothing; later entries are the deltas. Keep them append-only.
MIGRATIONS = (SCHEMA,)


class Database:
    """The connection pool, such as it is, and the schema that goes with it.

    Deliberately thin: this class knows how to open the file and what shape it
    should be in, and nothing about films. The queries live with the thing they
    are about -- Library, Thumbs -- which is the same division config.py draws
    between the value and the code that obeys it.
    """

    def __init__(self, path=None):
        self.path = DB_PATH if path is None else path
        self._local = threading.local()
        # Serialises the migration and nothing else. Ordinary reads and writes
        # rely on WAL and on busy_timeout, which is what they are for.
        self._lock = threading.Lock()

    def connect(self):
        """This thread's connection, opened on first use.

        Not closed anywhere. The threads that call this are the four daemon
        workers and the HTTP server's per-request threads, and the latter are
        short-lived enough that Python's own finalisation is the right owner --
        an explicit close would have to be threaded through every handler for
        no benefit that a process which lives for weeks can measure.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=5.0,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL so a poster extraction landing a blob does not block six phones
        # polling status. Persisted in the file itself, so this only actually
        # does anything the first time -- setting it every open costs nothing
        # and means a database restored from a copy is still in WAL.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Not the default, and every ON DELETE CASCADE above depends on it.
        # It is per-connection, so it has to be here rather than in the schema.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        self._local.conn = conn
        return conn

    def close(self):
        """Close this thread's connection, if it has one.

        The daemon never calls this -- see connect() -- but a test that makes
        thirty scratch databases in one process would otherwise leak thirty
        connections, and the warning that produces is a real signal being spent
        on a false alarm.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            conn.close()

    def migrate(self):
        """Bring the file up to SCHEMA_VERSION. Idempotent; safe to call at
        every start, which is exactly when it is called."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._lock:
            conn = self.connect()
            have = conn.execute("PRAGMA user_version").fetchone()[0]
            if have >= SCHEMA_VERSION:
                return have
            for step in MIGRATIONS[have:SCHEMA_VERSION]:
                with conn:
                    conn.executescript(step)
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            log("database at %s: schema %d -> %d", self.path, have,
                SCHEMA_VERSION)
            return SCHEMA_VERSION

    # -- the two shapes every caller wants ---------------------------------

    def query(self, sql, params=()):
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.connect().execute(sql, params).fetchone()

    def write(self):
        """A transaction, as a context manager: `with db.write() as conn:`.

        isolation_level=None means the driver never opens one on our behalf, so
        this is the only place a transaction begins -- which is what makes a
        whole library sync one commit rather than a thousand.
        """
        return self.connect()
