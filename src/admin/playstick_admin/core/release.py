"""Whether the stick is running what this machine would deploy. Qt-free.

Both sides of the comparison come from one script, scripts/release_id.py in the
repo root this app deploys from: Ansible runs it while deploying and the stick
reports the answer; this runs it now. Loaded from that root rather than kept
as a copy in the app, because two implementations of a hash only have to
differ by one byte to say "update available" forever.
"""

import importlib.util
import os
import time

from . import site_vars

SCRIPT = os.path.join("scripts", "release_id.py")

# A Device tab polled every few seconds should not re-read a few hundred
# kilobytes of roles and page each time; a file saved a moment ago shows up
# within this.
CACHE_SECONDS = 15

_cache = {"key": None, "at": 0.0, "value": None}


def _module(repo):
    path = os.path.join(repo, SCRIPT)
    spec = importlib.util.spec_from_file_location("playstick_release_id", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local(repo):
    """What a deploy from `repo` would report, or None when there is no repo
    to deploy from -- or it predates the script."""
    if not repo or not os.path.isfile(os.path.join(repo, SCRIPT)):
        return None
    now = time.monotonic()
    if _cache["key"] == repo and now - _cache["at"] < CACHE_SECONDS:
        return _cache["value"]
    value = _module(repo).compute(repo, site_vars.site_path())
    _cache.update(key=repo, at=now, value=value)
    return value


def forget():
    """After Settings is saved: the site values are part of the answer."""
    _cache["key"] = None


def compare(device, mine):
    """(state, sentence). state is "current", "update", "unknown" or "none":
    the last when there is nothing here to deploy from."""
    if mine is None:
        return "none", ("There is no playstick checkout to deploy from -- "
                        "set the repo root in Config, Settings.")
    if not device:
        return "update", ("The stick does not say which release it is running "
                          "-- it was deployed before releases were tracked. "
                          "Updating it puts release %s on it." % mine)
    if device == mine:
        return "current", "Up to date: release %s." % mine
    return "update", ("Update available: the stick is running release %s, "
                      "and this machine would deploy %s." % (device, mine))
