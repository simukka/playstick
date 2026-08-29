"""Posters from the device, cached on this machine by their etag.

WHY FETCH THEM AT ALL, when most of them were made on this machine

Because the two are not the same set. The device extracts a frame for any film
the admin app had no poster for, which is a poster this machine has never seen;
and a poster somebody replaced here has not reached the stick until a sync ran.
A Library tab that drew only local files would show neither difference, which
is exactly the difference somebody has this tab open to check.

WHY AN ETAG CACHE AND NOT A TIMER

A poster is tens of kilobytes and a library is a few hundred films, so the whole
set is a handful of megabytes -- small enough to hold, far too much to refetch
every time the tab is opened. The device names each one by sha1(bytes)[:16] and
answers a request carrying that etag with a 304 and no body, so a refresh costs
one small round trip per film and no image data at all.

The cache is keyed by the etag rather than the film id, deliberately: a poster
that changed has a different key, so nothing has to be invalidated and a stale
file cannot be served. Old entries are simply orphaned, and `sweep` removes
them when the cache grows past what a library could account for.
"""

import errno
import os
import tempfile

# Where the files go. Under the user's cache directory rather than the repo:
# these are fetched, not authored, and a `git status` full of JPEGs is how a
# cache announces it is in the wrong place.
def cache_dir():
    base = (os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "playstick-admin", "posters")


def path_for(etag, suffix=".img"):
    return os.path.join(cache_dir(), etag + suffix)


def cached(etag):
    """The file holding this etag's bytes, or None. An empty etag -- a film the
    device has no poster for -- is never cached and never fetched."""
    if not etag:
        return None
    path = path_for(etag)
    return path if os.path.isfile(path) else None


def store(etag, data):
    """Write bytes under their etag and return the path.

    tmp-then-rename, the same way everything else in this project writes a file
    it will later read: a half-written JPEG is indistinguishable from a corrupt
    one, and Qt would render it as nothing with no error anybody sees.
    """
    directory = cache_dir()
    os.makedirs(directory, exist_ok=True)
    path = path_for(etag)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".fetch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def fetch(client, film_id, etag):
    """One poster onto this machine, or None.

    Returns the cached path without asking the device when the etag is one we
    already hold, which is the point of the whole arrangement: the second time
    this tab is opened it makes no requests at all.
    """
    have = cached(etag)
    if have:
        return have
    data = client.thumb_bytes(film_id)
    if not data:
        return None
    return store(etag, data)


def sweep(keep_etags, limit=2000):
    """Drop cached posters nothing refers to any more.

    Only past `limit` files, because the cost of keeping a stale one is a few
    kilobytes and the cost of deleting one still in use is refetching it. A
    library of a few hundred films that gets re-postered a few times a year
    will never reach this.
    """
    directory = cache_dir()
    try:
        names = os.listdir(directory)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise
        return 0
    if len(names) <= limit:
        return 0
    keep = set(keep_etags)
    removed = 0
    for name in names:
        etag = os.path.splitext(name)[0]
        if etag.startswith(".") or etag in keep:
            continue
        try:
            os.unlink(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    return removed
