"""Preparing one film, from a row in the catalogue to a row on the stick.

This is the whole of what "Add to library" means, in order, in one place that
imports no Qt: probe, match, encode, place, move, record, push. The queue worker
is a thread that calls this and turns its progress calls into signals.

WHY THE ORCHESTRATION IS HERE AND NOT IN THE PREPARER

The preparer knows how to make a film. It does not know about sources, about a
share that is a different directory from the one the film came from, about a
database, or about a stick. Putting this sequence in there would mean the
preparer growing an opinion about all four, and the command line -- which is
still a supported way to prepare a library -- inheriting them.

Putting it here also means it is testable without ffmpeg: every expensive step
is a call into playstick_prep, and a test can replace those.

THE ORDER IS THE ERROR HANDLING

Nothing is recorded until everything has succeeded. A job that dies at any point
leaves files on the share that no row claims -- reclaimable, findable by
nas_store.orphans, and never a film the stick has been told about and cannot
play. The reverse order, recording first, would produce exactly that: a grid
entry that fails at the moment a child presses it.

The one deliberate exception is the push. A film whose push failed is prepared,
on the share and in the database, and the only thing wrong with it is that a box
in another room was asleep. Treating that as a failed preparation would mean
re-encoding a film because somebody switched the stick off, so it is recorded as
prepared-but-not-pushed and `sync.push_pending` is the recovery.
"""

import logging
import os
import shlex
import time

from .. import paths
from . import catalog, nas_store, prepared, sync
from .device_client import DeviceError

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402


LOG = logging.getLogger(__name__)


class PrepareError(Exception):
    pass


def build_args(source_root, staging, settings, extra=""):
    """The preparer's own argument namespace for one film.

    Assembled through prep.parse_args rather than by hand so that a new
    preparer flag reaches this without anybody remembering. `extra` is more of
    the preparer's own flags; the app passes none, and the tests use it to let
    their few-kilobyte films through the size and duration floors.
    """
    argv = ["--library", source_root, "--output", staging]
    if settings.tmdb_key:
        argv += ["--tmdb-key", settings.tmdb_key,
                 "--tmdb-language", settings.tmdb_language]
    if extra:
        argv += shlex.split(extra)
    return prep.parse_args(argv)


def _tmdb(args):
    if not args.tmdb_key:
        return None
    cache = paths.tmdb_cache_path()
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    return prep.Tmdb(args.tmdb_key, cache, args.tmdb_language)


def prepare(db, item, settings, client=None, report=None, extra=""):
    """Make one film and put it where the stick can reach it.

    `report(phase, text)` is called as it goes -- phase is one of the strings
    the queue panel shows. Returns a dict summarising what happened; raises
    PrepareError with a sentence somebody can read on anything fatal.
    """
    def say(phase, text=""):
        # Every phase goes to the terminal as well as to whoever is watching
        # the tab. A queue left running overnight is the case this is for: the
        # panel shows the film in front of it, the log shows the evening.
        LOG.info("%s: %s%s", short, phase, (" -- " + text) if text else "")
        if report:
            report(phase, text)

    ident = item["id"]
    short = item["title"] or item["rel"]
    began = time.time()
    source_root = item["source_path"] or os.path.dirname(item["abs_path"])
    nas_root = settings.nas_root
    staging = nas_store.staging_for(nas_root, ident)
    os.makedirs(staging, exist_ok=True)

    LOG.info("preparing %s (%s) from %s", short, ident, item["abs_path"])
    if not os.path.isfile(item["abs_path"]):
        raise PrepareError("%s is not there any more." % item["abs_path"])

    args = build_args(source_root, staging, settings, extra)

    # --- what can be learned without encoding anything ---------------------
    say("probing", os.path.basename(item["abs_path"]))
    fingerprint = catalog.fingerprint(item["size"], item["mtime"] or 0,
                                      args.verify, prep.META_VERSION)
    cached = catalog.load_probe(db, ident, fingerprint)
    # The preparer keys its cache by the relative path and guards it with a
    # fingerprint of its own. Handing it one entry rather than a whole library's
    # worth is what makes a re-run of an already-probed film free.
    state = {item["rel"]: {"fingerprint": [int(item["size"] or 0),
                                           int(item["mtime"] or 0)],
                           "verify_mode": args.verify,
                           "meta_version": prep.META_VERSION,
                           "movie": cached}} if cached else {}

    movie = prep.collect_one(item["abs_path"], item["rel"], args, state,
                             ident=ident)
    if movie is None:
        raise PrepareError("could not read %s" % item["rel"])
    if movie.get("unreadable"):
        raise PrepareError("ffprobe could not read it: %s"
                           % "; ".join(movie.get("verify_notes") or []))
    if not movie.get("verified_ok", True):
        raise PrepareError("; ".join(movie.get("verify_notes") or ["unverified"]))
    catalog.save_probe(db, ident, movie, fingerprint)

    # --- what it is called -------------------------------------------------
    # An episode is named by its path, not by a search. Its row keeps its own
    # title -- "Hospital", or "Episode 2" when the file says nothing more --
    # and the series it belongs to, which is what folds it into one poster on
    # the stick. The shelf key is the series', so the tile sits where the show
    # would on a shelf rather than wherever its first episode's name falls.
    episode = prep.episode_of(item["rel"])
    if episode is not None:
        movie["series"] = episode["series"]
        movie["season"] = episode["season"]
        movie["episode"] = episode["episode"]
        movie["title"] = (episode["episode_title"]
                          or "Episode %d" % episode["episode"])
        movie["sort_title"] = prep.episode_sort_title(episode)

    overrides = prepared.overrides(db)
    # Read once before the lookup, because a pinned tmdb_id is what makes the
    # lookup skip its own search; and applied again afterwards, because a
    # hand-typed title has to win over whatever TMDb just said.
    prep.apply_overrides(movie, overrides)
    # Not for an episode. The lookup searches FILMS, and an episode called
    # "Hospital" is a confident match for a film called Hospital -- a wrong
    # poster and a wrong plot, which is worse than a frame and no plot.
    tmdb = _tmdb(args) if episode is None else None
    if tmdb is not None:
        say("matching", movie.get("title") or "")
        why, _ok = prep.match_tmdb(movie, tmdb, overrides, args)
        tmdb.save()
        if why:
            # Not fatal, and deliberately so: an unmatched film keeps its
            # filename title and gets a frame for a poster, which is what an
            # unmatched film has always looked like. A wrong film on a shelf a
            # child picks from is worse than a plain one.
            say("matching", "no confident TMDb match: %s" % why)
    prep.apply_overrides(movie, overrides)

    # --- the expensive part ------------------------------------------------
    say("encoding", movie.get("title") or item["rel"])
    prep.prepare_one(movie, args, {"library": source_root, "output": staging},
                     tmdb)
    if movie.get("prepare_failed"):
        raise PrepareError("the encode failed; the log has the ffmpeg error")
    if prep.cancelled():
        raise PrepareError("stopped")

    # --- getting the film itself onto the share ----------------------------
    # After the encode, because when there IS an encode this is already done and
    # costs nothing, and when there is not this is the only thing that moves the
    # film at all.
    say("placing", movie.get("title") or "")
    if prep.place_media(movie, args, nas_root) is None:
        raise PrepareError("could not put the film on the share")

    poster_bytes, poster_mime = nas_store.read_poster(nas_root, ident)

    # --- out of staging ---------------------------------------------------
    rels = nas_store.artifact_rels(movie)
    staged = [r for r in rels
              if os.path.isfile(os.path.join(staging, r.replace("/", os.sep)))]
    say("moving", "%d file(s) onto the share" % len(staged))
    try:
        moved = nas_store.place(nas_root, ident, rels,
                                progress=lambda text: say("moving", text))
    except nas_store.NasError as exc:
        raise PrepareError(str(exc)) from exc

    # --- only now is it a film ---------------------------------------------
    source_rel = item["nas_rel"] or movie["media_rel"]
    prepared.record(db, ident, movie, movie["media_rel"], source_rel,
                    bytes_on_nas=moved,
                    poster=((poster_bytes, poster_mime,
                             movie.get("poster_source") or "prep")
                            if poster_bytes else None))
    nas_store.discard_staging(nas_root, ident)

    # --- and only now does the stick hear about it -------------------------
    pushed, push_error = False, None
    if client is not None and settings.device_url:
        say("pushing", movie.get("title") or "")
        try:
            sync.push_one(client, db, ident,
                          progress=lambda text: say("pushing", text))
        except DeviceError as exc:
            # Not a failed preparation. See the module docstring.
            push_error = str(exc)
            say("pushing", "the stick did not answer (%s); it is prepared and "
                           "waiting" % exc)
        else:
            pushed = True

    LOG.info("prepared %s in %.0fs: %s onto the share as %s%s",
             movie.get("title") or short, time.time() - began, _bytes(moved),
             movie["media_rel"],
             ", on the stick" if pushed
             else ", waiting for the stick" if push_error else "")
    return {"id": ident, "title": movie.get("title"), "bytes": moved,
            "pushed": pushed, "push_error": push_error,
            "media_rel": movie["media_rel"]}


def _bytes(num):
    """Sizes for the log. views.format.human_size does this for the window, and
    core does not import views -- it is four lines and one direction of
    dependency is worth more than sharing them."""
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f GB" % size


def refresh_metadata(db, ident, settings, client=None, report=None):
    """Re-derive what a film is called, without touching a byte of it.

    A metadata correction changes nothing on the share: the encode, the audio
    and the subtitles are the same files. Re-running a whole preparation to
    apply one would re-encode a film for the sake of a title, which is how the
    old app's "save and re-prepare" behaved and why it was an hour's wait.

    So this updates the record and the poster and pushes, and does no ffmpeg
    beyond what a poster costs. The film keeps its id, so the stick recognises
    it as the same row and keeps the resume position with it.
    """
    def say(phase, text=""):
        if report:
            report(phase, text)

    row = prepared.get(db, ident)
    if row is None:
        raise PrepareError("%s is not in the library." % ident)
    LOG.info("re-deriving the metadata for %s (%s), no ffmpeg",
             row["title"], ident)

    movie = {
        "id": ident,
        "title": row["title"], "sort_title": row["sort_title"],
        "year": row["year"], "rating": row["rating"], "plot": row["plot"],
        "duration": row["duration"], "width": row["width"],
        "height": row["height"], "vcodec": row["vcodec"],
        "acodec": row["acodec"], "tmdb_id": row["tmdb_id"],
        "hidden": bool(row["hidden"]),
        "series": row["series"], "season": row["season"],
        "episode": row["episode"],
        "genres": prepared.children(db, ident)["genres"],
        "subtitles": [{"rel": s["rel"], "lang": s["lang"]}
                      for s in prepared.children(db, ident)["subtitles"]],
        "phone_audio": [{"n": t["n"], "rel": t["rel"], "lang": t["lang"],
                         "title": t["title"], "channels": t["channels"],
                         "default": bool(t["is_default"]),
                         "offset": t["offset"]}
                        for t in prepared.children(db, ident)["audio"]],
    }
    say("matching", row["title"])
    prep.apply_overrides(movie, prepared.overrides(db))
    prepared.record(db, ident, movie, row["media_rel"], row["source_rel"],
                    bytes_on_nas=row["bytes_on_nas"])

    if client is not None and settings.device_url:
        say("pushing", movie.get("title") or "")
        sync.push_one(client, db, ident)
    return {"id": ident, "title": movie.get("title")}


def remove(db, ident, settings, client=None):
    """Take a film out of the library: off the stick, off the share, and back
    to `available` in the Prepare tab.

    In that order. The stick first, because a film whose files are gone but
    whose row is not is a tile a child presses that does nothing -- and the
    reverse, a row removed while the files linger, is only wasted space that
    nas_store.orphans can find.
    """
    rels = prepared.artifact_rels(db, ident)
    LOG.info("removing %s: %d file(s) on the share", ident, len(rels))
    if client is not None and settings.device_url:
        sync.remove_one(client, db, ident)
    removed, freed = nas_store.remove(settings.nas_root, rels)
    prepared.forget(db, ident)
    if catalog.get_item(db, ident) is not None:
        catalog.set_state(db, ident, catalog.AVAILABLE)
    LOG.info("removed %s: %d file(s) deleted, %s freed", ident, removed,
             _bytes(freed))
    return {"files": removed, "bytes": freed}
