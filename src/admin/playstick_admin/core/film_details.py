"""Everything this host knows about one film, for the window that shows it. Qt-free.

What there is to know is spread over half the schema -- the catalogue row, the
preparer's cached probe, what was prepared and pushed, the corrections, the
poster, every job that was ever run for it -- and the window wants it all at
once. Gathering it here keeps the view a drawing of a dict.

TMDb's own view of it -- poster, plot, rating -- is fetched separately, by
tmdb_preview, because it is the network and gather() is only the database.

WHY THE LIVE PROBE DOES NOT GO THROUGH THE PREPARER

`prep.ffprobe_json` runs its child through `prep.run`, which keeps the child in
a module global so a cancel can end it. A probe from this window while the
queue is encoding would overwrite that global, and cancelling the encode would
then end the wrong process -- or none. So the argv is the preparer's, and the
subprocess is this module's own.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

from .. import paths
from . import catalog, prepared
from .tmdb_client import TmdbSearch

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402


def gather(db, ident):
    """{section: dict-or-list} for one film, or None if there is no such item.

    Every value is plain JSON-able data: the poster is described rather than
    included, and the cached probe is parsed rather than left as a string.
    """
    item = catalog.get_joined(db, ident)
    if item is None:
        return None
    item = dict(item)
    probe_json = item.pop("probe_json", None)
    try:
        cached = json.loads(probe_json) if probe_json else None
    except ValueError:
        cached = {"unreadable": probe_json}

    made = prepared.get(db, ident)
    if made is not None:
        made = dict(made)
        made.update(prepared.children(db, ident))
        try:
            made["notes"] = json.loads(made["notes"]) if made["notes"] else []
        except ValueError:
            pass

    poster = db.one("SELECT mime, LENGTH(bytes) AS bytes, etag, source, "
                    "updated_at FROM poster WHERE item_id = ?", (ident,))
    jobs = [dict(r) for r in db.query(
        "SELECT * FROM job WHERE item_id = ? ORDER BY queued_at DESC",
        (ident,))]

    return {
        "item": item,
        "probe": cached,
        "prepared": made,
        "overrides": prepared.overrides(db, ident),
        "poster": dict(poster) if poster else None,
        "jobs": jobs,
    }


def tmdb_preview(key, language, details, timeout=20):
    """What TMDb says about one film -- title, year, rating, plot and the
    poster's bytes -- for the top of the window.

    `details` is what gather() returned. A pinned id wins, then the id the
    preparer matched; a film with neither gets the preparer's own gated lookup,
    so this never shows a match a preparation would have refused. Returns a
    dict whose "why" is set, and nothing else, when there is no match.
    Raises TmdbError for no key, and on a lookup that could not be made.
    """
    item = details["item"]
    made = details["prepared"] or {}
    pinned = details["overrides"].get("tmdb_id")
    tmdb_id = pinned or made.get("tmdb_id")
    client = TmdbSearch(key, language)
    if tmdb_id:
        meta = client.detail(tmdb_id)
        how = "pinned" if pinned else "matched when prepared"
    else:
        meta, why = client.lookup(item["title"] or item["rel"], item["year"],
                                  item["duration"])
        if why:
            return {"why": why}
        how = "what a preparation would match"
    poster = None
    if meta.get("poster_url"):
        try:
            with urllib.request.urlopen(meta["poster_url"],
                                        timeout=timeout) as resp:
                poster = resp.read()
        except (urllib.error.URLError, OSError):
            # The words are worth showing without the picture.
            poster = None
    return {
        "why": None,
        "how": how,
        "tmdb_id": meta.get("tmdb_id"),
        "title": meta.get("title"),
        "year": meta.get("year"),
        "rating": meta.get("rating"),
        "votes": meta.get("rating_votes"),
        "genres": meta.get("genres") or [],
        "plot": meta.get("plot") or "",
        "poster": poster,
    }


# Where the stills are taken, as fractions of the running time: clear of the
# studio logos at the start and the credits at the end.
FRAME_POINTS = (0.10, 0.26, 0.42, 0.58, 0.74, 0.90)


def frame_times(duration, points=FRAME_POINTS):
    """Seconds into the film to take a still at.

    Rounded to ten seconds so two copies of the same film -- whose running
    times differ by a few seconds of padding -- are grabbed at the same moment,
    and the stills are a comparison of the encodes rather than of the scenes.
    """
    if not duration or duration <= 0:
        return []
    return [int(round(duration * p / 10.0)) * 10 for p in points]


def frame(path, seconds, binary="", timeout=60):
    """One still, as PNG bytes, decoded at the file's own resolution.

    PNG because a still compressed again would show this app's artefacts on
    top of the encode's. `0:V:0` is the first video stream that is not cover
    art: an mp4 often carries its poster as a video stream of its own, and
    sometimes as the first one.
    """
    if not os.path.isfile(path):
        raise RuntimeError("%s is not there any more." % path)
    argv = [binary or prep.FFMPEG, "-nostdin", "-hide_banner", "-v", "error",
            "-ss", str(seconds), "-i", path, "-map", "0:V:0",
            "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        res = subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg was not found (%s). Install it, then "
                           "start the app again." % argv[0]) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg gave up after %ds" % timeout) from None
    if res.returncode != 0 or not res.stdout:
        lines = res.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else
                           "ffmpeg exited %d with no picture" % res.returncode)
    return res.stdout


def ffprobe(path, binary="", timeout=120):
    """ffprobe's own JSON for one file: format and every stream.

    Returns the parsed dict. Raises RuntimeError with ffprobe's last line of
    complaint when it cannot read the file, so a worker thread can hand the
    sentence straight to the window.
    """
    if not os.path.isfile(path):
        raise RuntimeError("%s is not there any more." % path)
    argv = [binary or prep.FFPROBE, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-show_chapters", path]
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("ffprobe was not found (%s). Install it, then "
                           "start the app again." % argv[0]) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe gave up after %ds" % timeout) from None
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else
                           "ffprobe exited %d" % res.returncode)
    try:
        return json.loads(res.stdout)
    except ValueError as exc:
        raise RuntimeError("unreadable ffprobe output: %s" % exc) from None
