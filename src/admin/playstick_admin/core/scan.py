"""Walking a source and writing down what is in it. Qt-free.

Discovery is the preparer's, deliberately and entirely: prep.discover decides
what a film is, and this only decides where to look and what to remember. That
matters more than it sounds. "A film" has to mean the same thing in the tab that
offers it, in the run that prepares it and in the index the stick is sent, and
the way to guarantee that is to have one implementation rather than three that
agree today.

WHAT A SCAN DOES NOT DO

It does not probe. ffprobe on every file across three mounts is minutes, and
nothing in the Prepare tab needs a codec to draw a row -- the title comes from
the filename by the same rules the preparer would use, and the rest fills in
when somebody actually asks for the film. A probe belongs to a preparation,
where somebody is already waiting.

It does not decide anything. Every file found is a row in `available`, which
means "nobody has said anything about this". A collection of four thousand
films produces four thousand such rows and no work at all.

EPISODIC FILES ARE RECORDED RATHER THAN DROPPED

The preparer refuses television, and it is right to: a single season becomes 24
near-identical tiles a child has to scroll past, and de-duplication starts
treating the episodes as copies of each other. But "where did my box set go" is
a real question, and a tab that had simply never mentioned the files cannot
answer it. They are written as `excluded` with the reason, and the tab can show
them on request.
"""

import os
import shlex
import time

from .. import paths
from . import catalog, identity

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402


def discovery_args(root, extra=""):
    """The preparer's own argument namespace, for discovery alone.

    Built through prep.parse_args rather than assembled by hand so that a new
    discovery flag reaches this without anybody remembering to add it -- and so
    that `prep_extra_args` means here exactly what it means everywhere else.
    """
    argv = ["--library", root]
    if extra:
        argv += shlex.split(extra)
    return prep.parse_args(argv)


def scan_source(db, source, nas_root, extra="", progress=None):
    """Walk one source and bring the catalogue up to date with it.

    Returns {"found", "episodic", "missing"}. `progress` is an optional callable
    taking one sentence, so the Qt layer can say what is happening without this
    module importing any of it.

    The order is deliberate: everything found is written first, and only then is
    anything not found marked missing. A walk that dies half way therefore
    leaves the catalogue short of a few new films rather than declaring the
    other three thousand gone.
    """
    def say(text):
        if progress:
            progress(text)

    root = source["path"]
    started = time.time()
    if not os.path.isdir(root):
        # Not an error worth failing on. An external drive that is unplugged is
        # an ordinary Tuesday, and the right answer is to leave every row it
        # contributed exactly where it is rather than declare the films gone.
        say("%s is not there right now; leaving what it offered alone" % root)
        return {"found": 0, "episodic": 0, "missing": 0, "unreachable": True}

    say("scanning %s..." % root)
    args = discovery_args(root, extra)
    try:
        found, episodic = prep.discover(root, args)
    except OSError as exc:
        say("could not walk %s: %s" % (root, exc))
        return {"found": 0, "episodic": 0, "missing": 0, "error": str(exc)}

    conn = db.write()
    with conn:
        for path in found:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            ident, nas_rel = identity.film_id(nas_root, path)
            try:
                stat = os.stat(path)
            except OSError:
                continue           # vanished between the walk and the stat
            catalog.upsert_item(
                db, ident, source["id"], rel, os.path.realpath(path),
                nas_rel=nas_rel, size=stat.st_size, mtime=stat.st_mtime,
                title=prep.clean_title(os.path.basename(rel)),
                year=prep.year_from_path(rel), conn=conn)

        for rel in episodic:
            # discover() reports pruned season FOLDERS too, with a trailing
            # separator. A directory is not an item -- there is nothing to
            # prepare and nothing to give an id to -- so only the files are
            # recorded, and the folder is left to the count.
            if rel.endswith(os.sep) or rel.endswith("/"):
                continue
            path = os.path.join(root, rel)
            ident, nas_rel = identity.film_id(nas_root, path)
            catalog.upsert_item(
                db, ident, source["id"], rel.replace(os.sep, "/"),
                os.path.realpath(path), nas_rel=nas_rel,
                title=prep.clean_title(os.path.basename(rel)),
                state=catalog.EXCLUDED,
                excluded_reason="episodic television", conn=conn)

    gone = catalog.mark_unseen_missing(db, source["id"], started)
    catalog.touch_scanned(db, source["id"])
    say("%s: %d film(s), %d episodic, %d no longer there"
        % (os.path.basename(root.rstrip(os.sep)) or root, len(found),
           len(episodic), gone))
    return {"found": len(found), "episodic": len(episodic), "missing": gone}


def scan_all(db, nas_root, extra="", progress=None):
    """Every enabled source, in turn. Returns the summed totals."""
    total = {"found": 0, "episodic": 0, "missing": 0, "sources": 0}
    for source in catalog.sources(db, enabled_only=True):
        result = scan_source(db, source, nas_root, extra, progress)
        for key in ("found", "episodic", "missing"):
            total[key] += result.get(key, 0)
        total["sources"] += 1
    return total


def roots(db):
    """Every enabled source's path, for the filesystem watcher."""
    return [row["path"] for row in catalog.sources(db, enabled_only=True)
            if os.path.isdir(row["path"])]
