"""What the admin database holds, and why any of it is here rather than there.

THE THREE MACHINES, AND WHAT EACH OF THEM MAY KNOW

The HOST runs this app. It can see everything -- a collection spread across
drives, mounts and other people's shares -- and it is the only one of the three
with cores to spare and a writable disk to work on.

The NAS is the narrow channel between the other two. The stick mounts it
read-only at /srv/movies and opens exactly four kinds of file off it: the film,
its re-encode, its subtitles and its per-language audio. Everything else that
used to live there -- the index, the overrides file, the probe cache, the TMDb
cache, the posters -- was a file the stick never opened, taking up space on the
one filesystem that has to be small enough to carry.

The STICK keeps what only it can know: where each film got to, which poster is
on the grid, and what somebody corrected from the sofa.

So this database is the host's half of that. It holds what the host learned and
the NAS should not have to carry: which directories to scan, what was found in
them, what ffprobe said, what is queued, what has been prepared and where it
landed, and the poster bytes waiting to be pushed. Nothing in here is on the
NAS, and nothing in here is a copy of something the stick owns.

WHY THE SHAPE MIRRORS src/server/playstick/db.py

Because the two are talking to each other. `prepared` is the row that becomes a
film on the stick, `override` is one row per edited field exactly as the daemon
stores it -- so that clearing an edit drops back to what the preparer derived
rather than to the last value anybody typed -- and `poster.etag` is computed the
same way `thumbs.etag_for()` computes it, because a sync compares the two across
a network. Where the shapes agree, they agree on purpose.

They are still two schemas and not one shared module, for the same reason
playstick_prep.py keeps its own copy of the daemon's title regexes: these
databases are on different machines with different lifetimes, and the day one of
them needs a column the other does not is not a day to spend on a migration for
a machine that did not change.
"""

# Bumped when SCHEMA below changes shape. A database at version N is brought
# forward by running every statement from N onwards; MIGRATIONS is append-only
# and there is no downgrade. Unlike the device's, this one is NOT rebuildable
# from somewhere else -- the metadata corrections in `override` and the record
# of what is on the NAS exist nowhere but here -- so a migration that drops
# data is a migration that loses it.
SCHEMA_VERSION = 2


SCHEMA = """
-- A directory on this host that is scanned for films.
--
-- The NAS is one of these only if somebody adds it. That is deliberate: the
-- share is where prepared films are PUT, and a library that scanned its own
-- output would offer to prepare the encodes it had just made.
CREATE TABLE source (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL UNIQUE,
  label       TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  added_at    REAL NOT NULL,
  scanned_at  REAL
);

-- Every candidate a scan found, whether or not anybody wants it on the stick.
-- This is the table the Prepare tab draws, and holding it rather than
-- recomputing it is most of the point: a walk of half a dozen roots over three
-- mounts is seconds at best, and the tab has to be able to open instantly and
-- say what it knew last time.
--
-- `id` is the identity the STICK will know this film by, decided at scan time
-- by core/identity.film_id -- not a row number, and not derived from anything
-- that changes when a title is corrected. See that module for why a file
-- already on the NAS keeps the id the old whole-library preparer gave it.
CREATE TABLE item (
  id            TEXT PRIMARY KEY,
  -- SET NULL and not CASCADE, and the difference is a whole class of bug.
  -- Removing a source means "stop scanning that directory". It does not mean
  -- "forget the films from it that are already on the NAS and on the stick" --
  -- and a cascade would mean exactly that: `prepared` would go with the item,
  -- leaving files on the share nothing references and rows on the stick nothing
  -- here can find to remove. An item whose source is gone is shown as orphaned
  -- and can still be taken back out of the library properly.
  source_id     INTEGER REFERENCES source(id) ON DELETE SET NULL,
  rel           TEXT NOT NULL,          -- relative to the source's path
  abs_path      TEXT NOT NULL,          -- resolved once, at scan time
  -- Whether the file is ALREADY under the NAS root. It decides the id scheme
  -- above, and it decides whether preparing this film has to copy 20 GB across
  -- a mount or can leave the bytes where they are.
  on_nas        INTEGER NOT NULL DEFAULT 0,
  nas_rel       TEXT,
  size          INTEGER NOT NULL DEFAULT 0,
  mtime         REAL,
  -- What is worth showing in a table before anybody has asked for the film.
  -- Filled from the filename at scan time and replaced by the probe when there
  -- is one, so a fresh scan draws something immediately.
  title         TEXT NOT NULL DEFAULT '',
  year          INTEGER,
  duration      REAL,
  width         INTEGER,
  height        INTEGER,
  vcodec        TEXT,
  acodec        TEXT,
  verified      TEXT,
  -- The preparer's collect() dict, verbatim. This is .playstick/prep-state.json
  -- brought in off the share, one row per film instead of one file for the
  -- library: probing is the slow half of preparing, and a cache the stick
  -- cannot read has no business on a mount the stick has to carry.
  probe_json        TEXT,
  probe_fingerprint TEXT,   -- size:mtime:verify_mode:meta_version
  -- available  nothing has been asked of it
  -- queued     a job exists
  -- preparing  that job is running now
  -- ready      prepared, on the NAS, and the stick has been told
  -- failed     a job gave up; `job.error` says why
  -- missing    the file was there at the last scan and is not there now
  -- excluded   discovery rejected it; `excluded_reason` says which rule
  state         TEXT NOT NULL DEFAULT 'available',
  excluded_reason TEXT,
  seen_at       REAL NOT NULL,
  UNIQUE (source_id, rel)
);
CREATE INDEX item_state ON item (state, title);

-- What was actually made for one film and put on the NAS. This is the row that
-- used to be an entry in playstick-library.json, and it is what a push to the
-- stick is built from.
--
-- media_rel and source_rel are BOTH relative to the NAS root, and that is the
-- load-bearing part. The daemon validates every path it is sent against its own
-- library root and refuses anything that resolves outside it, so a path
-- relative to somebody's home directory on this host is not a path the stick
-- can be told about. For a film whose original never reaches the share, the two
-- are the same file: see core/nas_store and playstick_prep.place_media.
CREATE TABLE prepared (
  item_id         TEXT PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
  media_rel       TEXT NOT NULL,
  source_rel      TEXT NOT NULL,
  title           TEXT NOT NULL,
  sort_title      TEXT NOT NULL DEFAULT '',
  year            INTEGER,
  -- Text because it is both a score like 7.8 and a certificate like "PG",
  -- exactly as on the device, where the column has the same argument.
  rating          TEXT,
  plot            TEXT,
  duration        REAL,
  width           INTEGER,
  height          INTEGER,
  vcodec          TEXT,
  acodec          TEXT,
  tmdb_id         INTEGER,
  tmdb_score      REAL,
  metadata_source TEXT,
  poster_source   TEXT,
  -- Corrections are baked in here rather than layered on at push time. The
  -- preparer has always applied them before writing an index, so this is the
  -- film as the preparer last decided it was -- and the device keeps its own
  -- override rows on top of that, which is how an edit made from the sofa
  -- survives a re-push of the base row.
  hidden          INTEGER NOT NULL DEFAULT 0,
  notes           TEXT,        -- JSON list: the preparer's verify notes
  bytes_on_nas    INTEGER NOT NULL DEFAULT 0,
  prepared_at     REAL NOT NULL,
  -- NULL until the stick has been told about this film. Not the same question
  -- as "is it prepared": a stick that was switched off when the job finished
  -- leaves a row that is on the NAS and unknown to the grid, and this column is
  -- how the next connection notices.
  pushed_at       REAL
);

CREATE TABLE prepared_genre (
  item_id  TEXT NOT NULL REFERENCES prepared(item_id) ON DELETE CASCADE,
  genre    TEXT NOT NULL,
  pos      INTEGER NOT NULL,
  PRIMARY KEY (item_id, genre)
);

CREATE TABLE prepared_subtitle (
  item_id  TEXT NOT NULL REFERENCES prepared(item_id) ON DELETE CASCADE,
  rel      TEXT NOT NULL,
  lang     TEXT NOT NULL DEFAULT 'und',
  pos      INTEGER NOT NULL,
  PRIMARY KEY (item_id, rel)
);

CREATE TABLE prepared_audio (
  item_id     TEXT NOT NULL REFERENCES prepared(item_id) ON DELETE CASCADE,
  n           INTEGER NOT NULL,
  rel         TEXT NOT NULL,
  lang        TEXT NOT NULL DEFAULT 'und',
  title       TEXT NOT NULL DEFAULT '',
  channels    INTEGER,
  is_default  INTEGER NOT NULL DEFAULT 0,
  offset      REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY (item_id, n)
);

-- The poster, as bytes, and deliberately NOT as a file on the share.
--
-- The stick serves posters out of its own `thumb` table and has never opened
-- one off the mount. So every JPEG in <nas>/.playstick/posters/ was a file
-- nothing read -- tens of megabytes of the share spent on an upload staging
-- area. Here they are the payload for PUT /api/admin/thumb and nothing else.
--
-- `etag` is sha1(bytes)[:16] and must stay in step with thumbs.etag_for() on
-- the daemon: the two are compared across a network, and a difference in how
-- they are derived means every poster uploaded on every sync, forever, with
-- nothing reporting anything wrong.
CREATE TABLE poster (
  item_id     TEXT PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
  mime        TEXT NOT NULL,
  bytes       BLOB NOT NULL,
  etag        TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT '',
  updated_at  REAL NOT NULL
);

-- The queue, and the reason it is a table rather than a list in memory.
--
-- One film is an hour of ffmpeg and a copy across a mount. A queue of twenty is
-- an evening, and an app that forgot it when somebody closed the window would
-- be an app nobody leaves running. A row per job also gives a failure somewhere
-- to keep its message and its log, which is the difference between "three
-- failed" and knowing which three and what happened.
CREATE TABLE job (
  id           INTEGER PRIMARY KEY,
  item_id      TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  -- queued | running | done | failed | cancelled
  state        TEXT NOT NULL DEFAULT 'queued',
  pos          INTEGER NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  log          TEXT,
  queued_at    REAL NOT NULL,
  started_at   REAL,
  finished_at  REAL
);
CREATE INDEX job_pending ON job (state, pos);

-- One row per EDITED FIELD, and the shape is the whole feature -- the same
-- argument the device's `override` table makes, because this is the other end
-- of it. The preparer derives a title from the filename; this holds what
-- somebody corrected. Kept apart so that clearing an edited field drops back to
-- what the file actually says rather than to the last value anybody typed: a
-- reset is a DELETE here, not a write of the old value.
--
-- The value is JSON so that `genres` -- the one editable field that is a list
-- -- fits in the same column as the scalars.
--
-- This replaces playstick-overrides.json, which lived on the share. The
-- daemon's copy of the same table is not a duplicate: an edit made from the
-- sofa lands there and is pulled back into here, so that the next preparation
-- honours it instead of re-deriving the old answer from the filename.
CREATE TABLE override (
  item_id  TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  field    TEXT NOT NULL,
  value    TEXT NOT NULL,
  PRIMARY KEY (item_id, field)
);

CREATE TABLE meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);
"""


# Episodes. A prepared row that is one episode of a series says which, so the
# stick can fold a season into one poster rather than twenty-four. All three
# stay NULL on a film. Columns rather than a table of their own because an
# episode is still exactly one playable file with one id -- its resume
# position, its soundtracks and its poster are all per episode, and making the
# series the row would mean re-plumbing every one of them.
EPISODES = """
ALTER TABLE prepared ADD COLUMN series TEXT;
ALTER TABLE prepared ADD COLUMN season INTEGER;
ALTER TABLE prepared ADD COLUMN episode INTEGER;
"""


# Applied in order to bring a database forward. Index 0 creates everything from
# nothing; later entries are the deltas. Keep them append-only.
MIGRATIONS = (SCHEMA, EPISODES)
