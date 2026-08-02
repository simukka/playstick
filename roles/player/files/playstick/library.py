"""The movie table: what is on the share, and what to call it.

Everything a client can address comes from here. The opaque ids the HTTP layer
accepts are the keys of this table, and Library._under_root is what proves a
path in the prep tool's index actually sits under the library root before any
other code is allowed to open it.
"""

import hashlib
import json
import os
import re
import threading
import time

from .config import (
    EXTENSIONS, INDEX_FILE, INDEX_SCHEMA, LIBRARY, MIN_SIZE,
    SCAN_DEPTH, SCAN_INTERVAL, SIDECAR_NAMES, SKIP_DIRS, SKIP_RE,
    TAG_RE, YEAR_RE, log
)


def clean_title(filename):
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"[._]+", " ", stem)
    stem = TAG_RE.sub("", stem)
    stem = re.sub(r"[-\s]+$", "", stem)
    stem = YEAR_RE.sub("", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip(" -[](){}")
    if not stem:
        stem = os.path.splitext(filename)[0]
    if stem.islower():
        stem = stem.title()
    return stem


def find_sidecar(path):
    """A poster the collection already carries, if there is one."""
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    candidates = [os.path.join(directory, stem + ext) for ext in (".jpg", ".png")]
    candidates += [os.path.join(directory, name) for name in SIDECAR_NAMES]
    for cand in candidates:
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:
            continue
    return None


class Library:
    """The movie list, cached because CIFS stat over this box's SDIO-attached
    Wi-Fi is what makes a rescan slow -- not the walk, the per-file stat."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._order = []
        self._available = False
        self._scanned_at = 0.0
        self._error = ""
        self._wake = threading.Event()

    def snapshot(self):
        with self._lock:
            return (list(self._order), dict(self._items), self._available,
                    self._scanned_at, self._error)

    def get(self, ident):
        with self._lock:
            return self._items.get(ident)

    def request_rescan(self):
        self._wake.set()

    def run(self, stopping):
        while not stopping.is_set():
            self._scan()
            self._wake.wait(SCAN_INTERVAL)
            self._wake.clear()

    def _scan(self):
        """The index if there is a usable one, a walk of the share otherwise.

        The two produce the same shape of item, and identical ids for the same
        film -- prep computes sha1(source_rel)[:16], which is what the walk
        below computes too -- so a poster already cached under one is still the
        right poster under the other, and a phone with the page open does not
        find everything renumbered.
        """
        items, order = None, None
        error = ""
        try:
            if INDEX_FILE and os.path.isfile(INDEX_FILE):
                items, order = self._read_index(INDEX_FILE)
        except OSError as exc:
            error = os.strerror(exc.errno) if exc.errno else str(exc)
            log("index unreadable (%s); walking the share instead", error)
        except Exception as exc:                     # noqa: BLE001
            log("index unusable (%s); walking the share instead", exc)

        if items is not None:
            with self._lock:
                self._items = items
                self._order = order
                self._available = True
                self._scanned_at = time.time()
                self._error = ""
            log("library: %d films from %s", len(order), INDEX_FILE)
            return

        self._walk()

    def _read_index(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        schema = int(data.get("schema") or 0)
        if schema > INDEX_SCHEMA:
            raise ValueError("schema %d is newer than this daemon understands "
                             "(%d) -- update playstick-web.py" % (schema, INDEX_SCHEMA))

        root = os.path.realpath(LIBRARY)
        items, order, missing = {}, [], 0
        for entry in data.get("movies") or []:
            ident = str(entry.get("id") or "")
            media = self._under_root(root, entry.get("rel"))
            if not ident or media is None:
                continue
            # An index that names a file which is not there is a library that
            # moved on without being re-prepped. Skip the entry rather than the
            # index: one deleted film should not cost the other ninety-nine.
            if not os.path.isfile(media):
                missing += 1
                continue
            items[ident] = {
                "id": ident,
                "title": entry.get("title") or os.path.basename(media),
                "path": media,
                "sidecar": None,
                "poster": self._under_root(root, entry.get("poster")),
                "subs": [p for p in (self._under_root(root, s.get("rel"))
                                     for s in entry.get("subtitles") or [])
                         if p and os.path.isfile(p)],
                # Not stat'ed, deliberately, where "subs" just above is.
                #
                # The difference is what a missing file costs. Subtitle paths
                # are handed to mpv on the command line and one that is not
                # there is a hard error at film start, so the check earns its
                # place. An audio track that has gone missing is a 404 on a
                # request nobody is blocked by, and the page has to be able to
                # explain that case anyway. Stat'ing them would mean up to four
                # more round trips per film over CIFS every scan interval,
                # which is the exact cost the index exists to avoid.
                "audio": [t for t in (
                    {"n": int(a.get("n") or 0),
                     "path": self._under_root(root, a.get("rel")),
                     "lang": (a.get("lang") or "und"),
                     "title": a.get("title") or "",
                     "channels": a.get("channels"),
                     "default": bool(a.get("default")),
                     "offset": float(a.get("offset") or 0.0)}
                    for a in entry.get("audio") or []
                ) if t["path"]],
                "year": entry.get("year"),
                "rating": entry.get("rating"),
                "genres": entry.get("genres") or [],
            }
            order.append(ident)
        if not items:
            raise ValueError("no usable entries")
        if missing:
            log("index: %d film(s) listed but not present -- re-run "
                "playstick-prep.py", missing)
        # The index's own order is kept rather than re-sorted: prep files by a
        # normalised title, so "The Fifth Element" sits under F the way it
        # would on a shelf, which the walk's plain sort cannot do.
        return items, order

    @staticmethod
    def _under_root(root, rel):
        """A library-relative path from the index, resolved and proved to be
        inside the library. The index is written on another machine and its
        paths are attacker-shaped input as far as this process is concerned --
        the same check start() makes before handing anything to mpv."""
        if not rel:
            return None
        candidate = os.path.realpath(os.path.join(root, rel))
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
        return None

    def _walk(self):
        items = {}
        order = []
        error = ""
        try:
            # os.walk swallows errors by default, which would report an
            # unreachable NAS as an empty library. onerror re-raises so the
            # UI can say "unavailable" instead of "you own no films".
            def boom(exc):
                raise exc

            for dirpath, dirnames, filenames in os.walk(LIBRARY, onerror=boom):
                rel_dir = os.path.relpath(dirpath, LIBRARY)
                depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth >= SCAN_DEPTH:
                    dirnames[:] = []
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d not in SKIP_DIRS]
                for name in sorted(filenames):
                    if not name.lower().endswith(EXTENSIONS) or name.startswith("."):
                        continue
                    if SKIP_RE and SKIP_RE.search(name):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    if MIN_SIZE and size < MIN_SIZE:
                        continue
                    rel = os.path.relpath(path, LIBRARY)
                    ident = hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:16]
                    items[ident] = {
                        "id": ident,
                        "title": clean_title(name),
                        "path": path,
                        "sidecar": find_sidecar(path),
                        # Always empty here, and stated rather than left to a
                        # .get() elsewhere: per-language audio is something
                        # playstick-prep.py extracts on a machine with cores.
                        # A library nobody has prepped has no sound for the
                        # phones, and the page says so.
                        "audio": [],
                    }
                    order.append(ident)
            available = True
        except OSError as exc:
            available = False
            error = os.strerror(exc.errno) if exc.errno else str(exc)
            log("library unavailable: %s", error)
        except Exception as exc:                     # noqa: BLE001
            available = False
            error = str(exc)
            log("library scan failed: %s", error)

        if available:
            order.sort(key=lambda i: items[i]["title"].lower())

        with self._lock:
            # A failed scan keeps the previous list rather than blanking the
            # grid: the NAS being briefly unreachable should not make a child's
            # films appear to have been deleted.
            if available:
                self._items = items
                self._order = order
            self._available = available
            self._scanned_at = time.time()
            self._error = error
        if available:
            log("library: %d films under %s", len(order), LIBRARY)
