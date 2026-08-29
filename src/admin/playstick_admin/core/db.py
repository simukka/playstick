"""Opening the admin database, and keeping it in the shape schema.py describes.

Deliberately thin: this class knows how to open the file and what version it
should be at, and nothing about films. The queries live with the thing they are
about -- see core/store.py -- which is the same division the daemon draws
between src/server/playstick/db.py and library.py.

ONE CONNECTION PER THREAD, FOR A SMALLER REASON THAN THE DAEMON'S

The daemon is a threaded HTTP server with four background workers. This is a Qt
app with two threads that touch the database: the GUI thread, which reads to
draw, and the queue worker, which writes for an hour at a time. sqlite3
connections still may not be shared across threads, so the arrangement is the
same even though the pressure is not -- and WAL still earns its place, because
the one thing this app must never do is freeze the window while a nine-hour
encode's bookkeeping commits.

THE FILE IS NOT REBUILDABLE, WHICH THE DAEMON'S IS

The stick's database can be thrown away and refilled by one sync from here.
This one cannot be refilled from anywhere: the metadata corrections in
`override` and the record of what is on the NAS exist nowhere else. So
synchronous is FULL rather than the daemon's NORMAL. The daemon trades a
possible lost commit against eMMC wear on a box somebody switches off at the
wall; here the storage is an ordinary disk and the commit is somebody's evening.
"""

import os
import sqlite3
import threading

from .. import paths
from .schema import MIGRATIONS, SCHEMA_VERSION


class Database:
    def __init__(self, path=None):
        self.path = paths.db_path() if path is None else path
        self._local = threading.local()
        # Serialises the migration and nothing else. Ordinary reads and writes
        # rely on WAL and on busy_timeout, which is what they are for.
        self._lock = threading.Lock()

    def connect(self):
        """This thread's connection, opened on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        # Not the default, and every ON DELETE above depends on it -- including
        # item.source_id's SET NULL, which without this silently does nothing.
        conn.execute("PRAGMA foreign_keys=ON")
        # Longer than the daemon's five seconds: the writer here can be holding
        # the connection while it reads a poster off a network mount.
        conn.execute("PRAGMA busy_timeout=15000")
        self._local.conn = conn
        return conn

    def close(self):
        """Close this thread's connection, if it has one. The app never calls
        this; a test that makes thirty scratch databases in one process would
        otherwise leak thirty connections."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            conn.close()

    def migrate(self):
        """Bring the file up to SCHEMA_VERSION. Idempotent; safe to call at
        every start, which is exactly when it is called."""
        with self._lock:
            conn = self.connect()
            have = conn.execute("PRAGMA user_version").fetchone()[0]
            if have >= SCHEMA_VERSION:
                return have
            for step in MIGRATIONS[have:SCHEMA_VERSION]:
                with conn:
                    conn.executescript(step)
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            return SCHEMA_VERSION

    # -- the shapes every caller wants --------------------------------------

    def query(self, sql, params=()):
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.connect().execute(sql, params).fetchone()

    def write(self):
        """A transaction, as a context manager: `with db.write() as conn:`.

        isolation_level=None means the driver never opens one on our behalf, so
        this is the only place a transaction begins -- which is what makes
        recording a finished film one commit rather than a dozen.
        """
        return self.connect()

    # -- the one-row key/value table ----------------------------------------

    def get_meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def set_meta(self, key, value):
        with self.write() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
