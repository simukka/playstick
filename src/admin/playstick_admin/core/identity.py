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
    The first root ever seen is recorded rather than questioned -- unless the
    rows already say which share they were named against, which they do in a
    database that predates this record.
    """
    if not nas_root:
        return "No NAS root is set. Settings -- the share, as it is mounted here."
    root = os.path.realpath(os.path.expanduser(nas_root))
    seen = db.get_meta("nas_root_seen") or _root_the_rows_imply(db)
    if not seen:
        db.set_meta("nas_root_seen", root)
        return None
    db.set_meta("nas_root_seen", seen)
    if seen == root:
        return None
    return ("This database was built against %s and the NAS root is now %s. "
            "Every film in it is named by a path relative to the first, so "
            "nothing here would reach the stick. Point it back, or start a "
            "new database." % (seen, root))


def _root_the_rows_imply(db):
    """The share an item already on it was recorded under, or None.

    An on-NAS row's abs_path is the share joined to its nas_rel, so stripping
    the one off the other gives back the root it was named against.
    """
    row = db.one("SELECT abs_path, nas_rel FROM item "
                 "WHERE on_nas = 1 AND nas_rel IS NOT NULL LIMIT 1")
    if row is None:
        return None
    suffix = "/" + row["nas_rel"]
    if not row["abs_path"].endswith(suffix):
        return None
    return row["abs_path"][:-len(suffix)]
