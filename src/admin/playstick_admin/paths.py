"""Where things are on disk, and how the preparer is imported.

The app runs in one of two ways, and nothing outside this module should need to
know which.

FROM A CHECKOUT (`make admin`). The package sits at src/admin/playstick_admin/
inside the playstick repo, and the repo is three directories up. The preparer
is src/prep/playstick_prep.py, put on sys.path from there, and the Config tab
edits the Ansible files beside it.

INSTALLED (a wheel, a .deb). The package is in some site-packages and there is
no repo above it -- three directories up is lib/ or /usr. The preparer is then
an ordinary top-level module installed alongside the package: the symlink at
src/admin/playstick_prep.py is listed in pyproject.toml's py-modules, and a
built wheel carries the file it points at. The Config tab has no files to edit
until Settings names a checkout, because group_vars and actl are the repo's,
not the app's -- a packaged copy of them would be a second inventory nobody
deploys from.
"""

import os
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_ABOVE = os.path.abspath(os.path.join(PKG_DIR, "..", "..", ".."))
PREP_DIR = os.path.join(_ABOVE, "src", "prep")

# The checkout this package is running from, or "" when it is installed.
# Judged by actl being there: a path three levels up always exists, and an
# installed copy must not mistake /usr for the repo.
REPO_ROOT = _ABOVE if os.path.isfile(os.path.join(_ABOVE, "actl")) else ""


def is_checkout(root):
    """True if root looks like a playstick repo -- one actl can run in."""
    return bool(root) and os.path.isfile(actl(root))


def actl(root):
    return os.path.join(root, "actl")


def group_vars(root):
    return os.path.join(root, "group_vars", "all.yml")


def host_vars(root):
    return os.path.join(root, "host_vars", "vivostick", "local.yml")


def ensure_prep_on_path():
    """Make `import playstick_prep` find the repo's copy when there is one.

    Installed, there is nothing to add: the module is already importable from
    site-packages, and a nonexistent directory at the front of sys.path is
    only a slower miss on every import after it.
    """
    if os.path.isfile(os.path.join(PREP_DIR, "playstick_prep.py")) \
            and PREP_DIR not in sys.path:
        sys.path.insert(0, PREP_DIR)


# Where this app keeps the things it owns. State rather than cache: the admin
# database holds every metadata correction anybody has made and the record of
# what is on the NAS, and a cache is by definition something a cleaner may
# delete. The daemon draws the same line for the same reason -- see the note
# above config._default_db() on the device side.
#
# The directory is playstick, shared with that daemon when a developer runs it
# by hand; the names inside do not collide.
def _state_base():
    return (os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))


def _cache_base():
    return (os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))


def state_dir():
    return os.path.join(_state_base(), "playstick")


def cache_dir():
    """Fetched things a cleaner may delete -- the device's posters."""
    return os.path.join(_cache_base(), "playstick")


def db_path():
    """The admin database. PLAYSTICK_ADMIN_DB overrides it, which is what the
    tests and a second profile on one machine both need."""
    return (os.environ.get("PLAYSTICK_ADMIN_DB")
            or os.path.join(state_dir(), "admin.db"))


def tmdb_cache_path():
    """The preparer's TMDb cache, here rather than on the share.

    It used to live at <library>/.playstick/tmdb-cache.json, which put a file
    nothing on the stick ever opens onto the one filesystem this app is trying
    to keep to films.
    """
    return os.path.join(state_dir(), "tmdb-cache.json")

