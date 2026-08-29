#!/usr/bin/env python3
"""Put the development container's sample films into its database.

WHY THIS EXISTS AT ALL

The appliance has no idea what is on the share. Its library is a table the
desktop admin app fills over /api/admin/films, and the walk that used to
discover films is gone -- see src/server/playstick/library.py for why.

That is correct on the device and useless in a container, which has no admin
app, no Qt, and a library that gui-entrypoint.sh generated thirty seconds ago.
So this stands in for the admin app: it finds the films the same way the
preparer would, builds the same payload the admin app would POST, and writes it
through the same Library.sync the HTTP route calls. The only step it skips is
the HTTP, because the daemon it would be talking to has not started yet.

It prefers a real prep index when there is one -- run playstick-prep.py in the
container and the posters, subtitles and headphone audio it made all come
through. Without one, every film is listed against its original file with no
poster and no phone audio, which is exactly what an unprepared film looks like
on the device too.

NOT FOR THE DEVICE. Nothing installs this there and nothing should: on the
appliance the admin app is the writer, over HTTP, and a second path into the
film table is a second thing to keep in step with the first.
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
for extra in ("/usr/local/lib", "/usr/local/lib/playstick-prep"):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import playstick_prep as prep                                    # noqa: E402
from playstick.db import Database                                # noqa: E402
from playstick.library import Library                            # noqa: E402
from playstick.thumbs import Thumbs                              # noqa: E402


def from_index(library):
    path = os.path.join(library, prep.INDEX_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        print("seed: ignoring unreadable index (%s)" % exc, file=sys.stderr)
        return None


def from_walk(library):
    """An index shaped by hand, from prep's own discovery rules.

    prep.discover and prep.clean_title rather than a second idea of what counts
    as a film: "new" here has to mean exactly what the next prep run would pick
    up, or the container would show a library the device never could.
    """
    args = prep.parse_args(["--library", library, "--min-size-mb", "0"])
    found, _episodic = prep.discover(library, args)
    movies = []
    for path in sorted(found):
        rel = os.path.relpath(path, library).replace(os.sep, "/")
        movies.append({
            "id": prep.file_id(rel),
            "source_rel": rel,
            "rel": rel,
            "title": prep.clean_title(os.path.basename(rel)),
            "sort_title": prep.normalize_title(
                prep.clean_title(os.path.basename(rel))),
            "year": prep.guess_year(os.path.basename(rel)),
        })
    return {"movies": movies}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--library", default=os.environ.get(
        "PLAYSTICK_GUI_LIBRARY_PATH", "/srv/movies"))
    parser.add_argument("--database", default=None,
                        help="defaults to whatever the settings file names")
    parser.add_argument("--reconcile", action="store_true",
                        help="also remove films that are no longer there")
    args = parser.parse_args(argv)

    index = from_index(args.library)
    walked = index is None
    if walked:
        index = from_walk(args.library)
    films = prep.device_films(index)

    db = Database(args.database)
    db.migrate()
    library = Library(db)
    written, removed = library.sync(films, reconcile=args.reconcile)

    # The posters too, when prep made any: on the device these go up as bytes
    # through PUT /api/admin/thumb/<id>, and a container that skipped them
    # would show placeholders for films that have a real poster on disk.
    thumbs = Thumbs(db, library, None)
    posters = 0
    for movie in index.get("movies") or []:
        path = prep.poster_path(args.library, movie)
        if not path or thumbs.have(movie["id"]):
            continue
        with open(path, "rb") as handle:
            thumbs.store(movie["id"], handle.read(), "image/jpeg",
                         source="admin")
        posters += 1

    print("seed: %d film(s)%s, %d poster(s), from %s"
          % (written, ", %d removed" % removed if removed else "", posters,
             "a walk" if walked else prep.INDEX_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
