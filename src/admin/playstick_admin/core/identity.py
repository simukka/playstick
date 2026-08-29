"""What a film is called by the one machine whose opinion is load-bearing.

A film's id is sixteen hex characters, and three separate things on the stick
hang off it: the poster on the children's grid, every metadata correction
anybody has made, and where each film got to. None of those is derivable from
anything else. So an id is not a convenience here -- it is the join between a
file on this host and a row on a box in another room, and a restructure that
quietly re-issued them would look exactly like one that had worked, right up
until somebody noticed every resume position was gone.

WHY THERE ARE TWO RULES AND NOT ONE

Before this, there was one library, and `prep.file_id` hashed a film's path
relative to it. Every film already on a stick was named that way.

Now there are many sources, and most of them are nowhere near the share. A
single rule over "path relative to its own source root" would collide the
moment two sources both offered `Hook/Hook.mkv`, and a single rule over the
absolute path would re-issue an id for every film already synced -- which is
the cost above, paid for nothing.

So: a file already under the NAS root keeps the name the old preparer gave it.
Everything else is named by its absolute path on this host. The two spaces
cannot collide, because one is a hash of a relative path and the other of an
absolute one, and neither rule ever sees the other's input.

WHAT THIS COSTS, SAID PLAINLY

Moving a file re-keys it. Under the NAS that was always true. Off the NAS it
means a film that moves between drives comes back as a new candidate, and the
old one goes `missing` -- which is honest, and is also the only signal this app
gets that anything happened. Re-pointing `nas_root` at a different directory
invalidates every id in the database at once; `guard_nas_root` below is what
notices, because the failure is otherwise silent and total.
"""

import os

from .. import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402


def nas_relative(nas_root, abs_path):
    """`abs_path` as a path relative to the share, or None if it is not on it.

    Resolved with realpath on both sides, so a symlink into the share counts as
    being on the share and a symlink out of it does not -- the same judgement
    the daemon's Library._under_root makes about a path it has been sent, and
    made the same way for the same reason.
    """
    if not nas_root or not abs_path:
        return None
    root = os.path.realpath(os.path.expanduser(nas_root))
    target = os.path.realpath(os.path.expanduser(abs_path))
    if target == root:
        return None
    if not target.startswith(root + os.sep):
        return None
    return os.path.relpath(target, root).replace(os.sep, "/")


def film_id(nas_root, abs_path):
    """The sixteen characters the stick will know this film by.

    Returns (id, nas_rel). `nas_rel` is the path relative to the share when the
    file is already on it, and None otherwise -- which is the same question
    "does preparing this film have to copy it across a mount" asks, so the
    caller wants both answers and it costs one realpath to give them together.
    """
    rel = nas_relative(nas_root, abs_path)
    if rel is not None:
        return prep.file_id(rel), rel
    return prep.file_id(os.path.realpath(os.path.expanduser(abs_path))), None


# Where a film that is not already on the share ends up once it is. Both of
# these are relative to the NAS root, which is the only frame of reference the
# stick has: it mounts the share at /srv/movies and resolves every path it is
# given against that, refusing anything that lands outside.
MEDIA_DIR = "%s/media" % prep.WORK_DIR


def media_rel_for(ident, ext=".mp4"):
    return "%s/%s%s" % (MEDIA_DIR, ident, ext)


def guard_nas_root(db, nas_root):
    """Refuse to keep going against a share this database was not built for.

    Every `nas_rel` and half the ids in the file are relative to one directory.
    Point the app at a second share and nothing raises: the scan works, the
    encode works, and the push sends the stick paths that resolve to nothing on
    its own mount -- which the daemon answers by dropping the row and logging a
    line nobody is reading. The whole library goes quietly missing.

    Returns None when the root is fine, or a sentence saying what is wrong.
    The first root ever seen is recorded rather than questioned.
    """
    if not nas_root:
        return "No NAS root is set. Settings -- the share, as it is mounted here."
    root = os.path.realpath(os.path.expanduser(nas_root))
    seen = db.get_meta("nas_root_seen")
    if not seen:
        db.set_meta("nas_root_seen", root)
        return None
    if seen == root:
        return None
    return ("This database was built against %s and the NAS root is now %s. "
            "Every film in it is named by a path relative to the first, so "
            "nothing here would reach the stick. Point it back, or start a "
            "new database." % (seen, root))
