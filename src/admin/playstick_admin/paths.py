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
