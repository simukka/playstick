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

EPISODES ARE OFFERED LIKE FILMS

The preparer's command line still refuses television by default, and for the
reason it gives: in a whole-library run a season becomes 24 near-identical tiles
and de-duplication treats the episodes as copies of each other. Neither applies
here. Nothing is prepared until somebody picks it, there is no de-duplication,
and the stick folds every episode of a series into one poster -- so discovery is
run with --allow-episodes, and an episode is an ordinary `available` row whose
title says which one it is.

Episodes an older scan wrote as `excluded` are brought back to `available` by
the next one; see catalog.upsert_item.
"""

import logging
import os
import shlex
import time

from .. import paths
from . import catalog, identity

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

LOG = logging.getLogger(__name__)


class WrongShare(Exception):
    """nas_root is not the share this database's ids were named against.

    Raised before anything is written. Scanning on would give every film on the
    share a new id under the same (source, rel) -- which the UNIQUE constraint
    turns into a crash half way through a source, and which would orphan every
    poster, correction and resume position on the stick if it did not.
    """


def discovery_args(root, extra=""):
    """The preparer's own argument namespace, for discovery alone.

    Built through prep.parse_args rather than assembled by hand so that a new
    discovery flag reaches this without anybody remembering to add it. `extra`
    is more of the preparer's flags: the app passes none, and the tests use it
    to let their few-kilobyte films through the size and duration floors.
    """
    # First, so that an explicit flag in `extra` still has the last word.
    argv = ["--library", root, "--allow-episodes"]
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
        # The same sentence to the tab and to the terminal. A scan of three
        # mounts is the longest thing this app does that is not an encode, and
        # the only sign of it used to be a label on a tab.
        LOG.info("%s", text)
        if progress:
            progress(text)

    problem = identity.guard_nas_root(db, nas_root)
    if problem:
        raise WrongShare(problem)

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
    episodic = []
    try:
        # The second list is what discovery skipped as television, which
        # --allow-episodes leaves empty; the episodes are in the first.
        found, _skipped = prep.discover(root, args)
    except OSError as exc:
        say("could not walk %s: %s" % (root, exc))
        return {"found": 0, "episodic": 0, "missing": 0, "error": str(exc)}

    LOG.info("%s: the walk found %d file(s) in %.1fs; writing them down",
             root, len(found), time.time() - started)
    conn = db.write()
    with conn:
        for path in found:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            ident, nas_rel = identity.film_id(nas_root, path)
            try:
                stat = os.stat(path)
            except OSError:
                continue           # vanished between the walk and the stat
            episode = prep.episode_of(rel)
            if episode is not None:
                episodic.append(rel)
            catalog.upsert_item(
                db, ident, source["id"], rel, os.path.realpath(path),
                nas_rel=nas_rel, size=stat.st_size, mtime=stat.st_mtime,
                title=(episode_label(episode) if episode
                       else prep.clean_title(os.path.basename(rel))),
                year=prep.year_from_path(rel), conn=conn)

    gone = catalog.mark_unseen_missing(db, source["id"], started)
    catalog.touch_scanned(db, source["id"])
    films = len(found) - len(episodic)
    say("%s: %d film(s), %d episode(s), %d no longer there"
        % (os.path.basename(root.rstrip(os.sep)) or root, films,
           len(episodic), gone))
    return {"found": films, "episodic": len(episodic), "missing": gone}


def episode_label(episode):
    """What the Prepare tab calls an episode before anybody has prepared it:
    "Bluey S01E02 Hospital". The SxxEyy form because it is what the files are
    named with, so it is what somebody searching the tab will type."""
    label = "%s S%02dE%02d" % (episode["series"], episode["season"],
                               episode["episode"])
    if episode["episode_title"]:
        label += " " + episode["episode_title"]
    return label


def scan_all(db, nas_root, extra="", progress=None):
    """Every enabled source, in turn. Returns the summed totals."""
    began = time.time()
    total = {"found": 0, "episodic": 0, "missing": 0, "sources": 0}
    for source in catalog.sources(db, enabled_only=True):
        result = scan_source(db, source, nas_root, extra, progress)
        for key in ("found", "episodic", "missing"):
            total[key] += result.get(key, 0)
        total["sources"] += 1
    LOG.info("scanned %d source(s) in %.1fs: %d film(s), %d episodic, "
             "%d no longer there", total["sources"], time.time() - began,
             total["found"], total["episodic"], total["missing"])
    return total


def roots(db):
    """Every enabled source's path, for the filesystem watcher."""
    return [row["path"] for row in catalog.sources(db, enabled_only=True)
            if os.path.isdir(row["path"])]
