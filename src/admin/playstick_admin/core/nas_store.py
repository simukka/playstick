"""Putting a finished film onto the share, and taking one back off. Qt-free.

WHY A FILM IS MADE SOMEWHERE ELSE FIRST

An encode is hours of small writes. The share is a CIFS mount over whatever
wireless the house has, and it is the slow part of every operation in this app.
So a preparation runs against a staging directory on local disk and only the
finished artifacts cross the mount -- once each, sequentially, at whatever the
link will do.

The staging layout is deliberately byte-identical to the share's. The preparer
writes .playstick/media/<id>.mp4 relative to whatever output root it is given,
so a move is `for rel in rels: <staging>/rel -> <nas>/rel` and there is no path
arithmetic anywhere to get wrong. The paths recorded in the database and sent to
the stick are those same rels.

WHAT IS ATOMIC HERE AND WHAT IS NOT, WHICH MATTERS

Each FILE lands atomically: it is written to `<dest>.part` and renamed into
place. That is not tidiness. The stick mounts the share with actimeo=60, so a
file written in place can be served at a stale size for a minute afterwards --
and a wrong Content-Length is a phone that stops halfway through a film. The
preparer already writes this way for the same reason; this keeps it true across
the mount.

The SET of files does not land atomically, and cannot: there is no transaction
over a filesystem. So the ordering is the guarantee instead. The database row
is written only after every move has succeeded, which means a crash in the
middle leaves bytes on the share that nothing references -- wasted space, never
a broken library, and never a film the stick has been told about but cannot
play. `sweep_orphans` reclaims them, and it is the same walk a removal uses.

FREE SPACE IS CHECKED BEFORE ANY OF IT STARTS

ENOSPC six gigabytes into a copy over CIFS is a failure that costs an hour and
leaves debris. It is also entirely predictable: the sizes are known before the
first byte moves.
"""

import errno
import os
import shutil

from .. import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

# The directories under the share that this app owns. Everything else there is
# somebody's film collection and is never touched -- a sweep that walked the
# whole share would be one bug away from deleting it.
OWNED = ("media", "subs", "audio")


class NasError(Exception):
    pass


def staging_for(staging_root, ident):
    """Where one film is prepared. A directory per film rather than one shared
    tree, so that abandoning a job is `rmtree` on a path that belongs to nothing
    else."""
    return os.path.join(staging_root, ident)


def artifact_rels(movie):
    """Everything a finished preparation produced, as output-relative paths.

    Read off the movie dict the preparer filled in rather than by walking the
    staging directory: a walk would also pick up a half-written `.part` from a
    step that failed, and copying that onto the share is how a corrupt file
    gets a row pointing at it.
    """
    rels = []
    if movie.get("media_rel"):
        rels.append(movie["media_rel"])
    for sub in movie.get("subtitles") or ():
        if sub.get("rel"):
            rels.append(sub["rel"])
    for track in movie.get("phone_audio") or ():
        if track.get("rel"):
            rels.append(track["rel"])
    return [r.replace(os.sep, "/") for r in rels]


def _order(rels):
    """Subtitles and audio before the film.

    The daemon stats every subtitle path before it starts mpv -- a missing one
    is a hard error at film start, not a missing feature -- so they must be
    there by the time anything can play. The film is also the largest by two
    orders of magnitude, and putting it last means an interrupted move leaves
    the cheap things done rather than the expensive one half-copied.
    """
    return sorted(rels, key=lambda r: "/media/" in r)


def check_space(nas_root, needed, margin=0.05):
    """Refuse a move that obviously will not fit. Returns None or a sentence."""
    try:
        free = shutil.disk_usage(nas_root).free
    except OSError as exc:
        return "Could not check free space on %s: %s" % (nas_root, exc)
    want = int(needed * (1 + margin))
    if free >= want:
        return None
    return ("%s needs about %s free and has %s."
            % (nas_root, prep.human_size(want), prep.human_size(free)))


def place(staging_root, nas_root, ident, rels, progress=None):
    """Move one film's artifacts onto the share. Returns the bytes moved.

    Raises NasError on the first failure, having cleaned up its own `.part`.
    Files already moved are left where they are: the caller has not written a
    row yet, so they are orphans the sweep will find, and deleting them here
    would mean a retry re-copying what it had already got across.
    """
    staging = staging_for(staging_root, ident)
    moved = 0
    for rel in _order(rels):
        src = os.path.join(staging, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            # Nothing to move: place_media() writes a film that needed no
            # encode straight onto the share, because a copy has no reason to
            # go via local disk first and would otherwise cost two.
            continue
        dest = os.path.join(nas_root, rel.replace("/", os.sep))
        if progress:
            progress("moving %s onto the share" % os.path.basename(rel))
        moved += _move_one(src, dest)
    return moved


def _move_one(src, dest):
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        try:
            # Free when staging and the share happen to be one filesystem,
            # which is what somebody who staged onto the NAS deliberately gets.
            os.replace(src, part)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copyfile(src, part)
            # Through to the platter before the rename claims it is there. The
            # rename is what makes the file visible at its real name, and a
            # rename that outran the data would publish a short file.
            with open(part, "rb") as handle:
                os.fsync(handle.fileno())
        size = os.path.getsize(part)
        os.replace(part, dest)
    except OSError as exc:
        _discard(part)
        raise NasError("could not put %s on the share: %s"
                       % (os.path.basename(dest), exc)) from exc
    return size


def _discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def remove(nas_root, rels):
    """Delete one film's artifacts. Returns (removed, bytes).

    Only paths under the directories this app owns are touched -- see OWNED.
    A film that was already on the share plays from where it lies and its own
    file is somebody's, not this app's, so a removal must never reach it.
    """
    removed, freed = 0, 0
    for rel in rels:
        if not _is_owned(rel):
            continue
        path = os.path.join(nas_root, rel.replace("/", os.sep))
        try:
            freed += os.path.getsize(path)
            os.unlink(path)
            removed += 1
        except OSError:
            continue
    _prune_empty(nas_root)
    return removed, freed


def _is_owned(rel):
    parts = rel.replace(os.sep, "/").split("/")
    return len(parts) > 2 and parts[0] == prep.WORK_DIR and parts[1] in OWNED


def _prune_empty(nas_root):
    """Take away the per-film audio directories a removal empties. Only under
    .playstick/audio/, and only when empty -- nothing here removes a directory
    that has anything in it."""
    audio = os.path.join(nas_root, prep.WORK_DIR, "audio")
    try:
        names = os.listdir(audio)
    except OSError:
        return
    for name in names:
        path = os.path.join(audio, name)
        try:
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        except OSError:
            continue


def orphans(db, nas_root):
    """Files under the directories this app owns that no prepared film claims.

    What a crash mid-move leaves, and what a removal that failed part way
    leaves. Reported rather than deleted: this walks a share that is somebody's
    film collection, and a function that both decides and deletes is one bug
    away from a very bad afternoon.
    """
    claimed = set()
    for row in db.query("SELECT media_rel, source_rel FROM prepared"):
        claimed.add(row["media_rel"])
        claimed.add(row["source_rel"])
    for table in ("prepared_subtitle", "prepared_audio"):
        claimed |= {r["rel"] for r in db.query("SELECT rel FROM %s" % table)}

    found = []
    for name in OWNED:
        base = os.path.join(nas_root, prep.WORK_DIR, name)
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, nas_root).replace(os.sep, "/")
                # A ".part" is always debris: the rename that would have made
                # it real never happened.
                if rel in claimed and not rel.endswith(".part"):
                    continue
                try:
                    found.append((rel, os.path.getsize(path)))
                except OSError:
                    continue
    return sorted(found)


def discard_staging(staging_root, ident):
    """Throw away one film's working directory.

    Only its own. A sweep of the whole staging root after a successful job
    would also take the half-made encodes belonging to jobs that FAILED and are
    waiting to be retried -- and those are the ones where an hour of libx264 is
    sitting on disk ready to be skipped.
    """
    shutil.rmtree(staging_for(staging_root, ident), ignore_errors=True)


def sweep_staging(staging_root, keep=()):
    """Clear the debris of a crash. `keep` is the ids still worth resuming.

    Nothing in staging is irreplaceable -- it is either half-made or already
    copied somewhere that matters -- so this is safe to run at every start, and
    that is when it runs.
    """
    keep = set(keep)
    removed = 0
    try:
        names = os.listdir(staging_root)
    except OSError:
        return 0
    for name in names:
        if name in keep:
            continue
        path = os.path.join(staging_root, name)
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def read_poster(staging_root, ident):
    """The poster the preparation made, as bytes, and its type.

    Read out of staging and never moved onto the share. The stick serves
    posters from its own database and has never opened one off the mount, so a
    JPEG on the share is tens of kilobytes nothing will read -- times a few
    hundred films.
    """
    base = os.path.join(staging_for(staging_root, ident), prep.WORK_DIR,
                        "posters")
    for name, mime in (("%s.jpg" % ident, "image/jpeg"),
                       ("%s.png" % ident, "image/png")):
        path = os.path.join(base, name)
        try:
            with open(path, "rb") as handle:
                return handle.read(), mime
        except OSError:
            continue
    return None, None
