"""Where things are on disk, and how the preparer is imported.

The admin app lives at src/admin/playstick_admin/ inside the playstick repo. It
drives the preparer, which lives at src/prep/playstick_prep.py -- a module whose
filename it cannot `import` directly, so the path is added here once.
"""

import os
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(PKG_DIR, "..", "..", ".."))
PREP_DIR = os.path.join(REPO_ROOT, "src", "prep")
ACTL = os.path.join(REPO_ROOT, "actl")
GROUP_VARS = os.path.join(REPO_ROOT, "group_vars", "all.yml")
HOST_VARS = os.path.join(REPO_ROOT, "host_vars", "vivostick", "local.yml")


def ensure_prep_on_path():
    """Make `import playstick_prep` work from anywhere in the app."""
    if PREP_DIR not in sys.path:
        sys.path.insert(0, PREP_DIR)


# Where this app keeps the things it owns. State rather than cache: the admin
# database holds every metadata correction anybody has made and the record of
# what is on the NAS, and a cache is by definition something a cleaner may
# delete. The daemon draws the same line for the same reason -- see the note
# above config._default_db() on the device side.
def state_dir():
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "playstick-admin")


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
