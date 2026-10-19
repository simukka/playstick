"""Finding ffmpeg and ffprobe, so that nobody has to say where they are.

They used to be settings, empty by default, which meant "whatever PATH finds".
That is right from a terminal and wrong from a desktop launcher: a session
started from the menu often has a PATH without /usr/local/bin, ~/.local/bin or
/snap/bin, so a build somebody installed by hand worked under `make admin` and
was "not found" from the icon. Looking in those places as well closes that gap
without a setting.

$FFMPEG and $FFPROBE still win, as they do for the preparer on its own.
"""

import logging
import os
import shutil

from . import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

LOG = logging.getLogger(__name__)

# After PATH, in this order. Where a hand-installed or packaged build lands
# when it is not on a launcher's PATH.
FALLBACK_DIRS = (
    "/usr/local/bin",
    os.path.join(os.path.expanduser("~"), ".local", "bin"),
    "/snap/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
)


def find(name, env):
    """The path to run for `name`, or None if it is nowhere to be found."""
    explicit = os.environ.get(env)
    if explicit:
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for directory in FALLBACK_DIRS:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def configure():
    """Point the preparer at what was found, and say what that was. Once, at
    startup; a tool missing here is a warning, not a refusal to start, because
    most of the app works without either."""
    for name, env, attr in (("ffmpeg", "FFMPEG", "FFMPEG"),
                            ("ffprobe", "FFPROBE", "FFPROBE")):
        path = find(name, env)
        if path:
            setattr(prep, attr, path)
            LOG.info("%s: %s", name, path)
        else:
            LOG.warning("%s was not found; nothing can be prepared until it "
                        "is installed", name)
