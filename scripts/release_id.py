#!/usr/bin/env python3
"""The identity of what a deploy would put on the stick, as twelve hex digits.

    scripts/release_id.py <repo> [<site.yml>]

Run in two places, and it has to agree with itself in both, which is why it is
one file rather than two implementations:

  by Ansible, on the control node, while deploying -- the player role writes
  the answer into the stick's settings.toml, and the daemon reports it as
  "release" in /api/status;

  by the desktop app, against the repo root it deploys from -- and when the
  two differ, the Device tab offers to update the stick.

WHAT IS IN IT. Everything a deploy installs or decides with: the playbook, the
inventory, group_vars, every role, the daemon's code and the phone page -- and
the site values the app hands Ansible (the optional second argument), so that
changing the NAS in Settings is an update too. The NAS password is not in that
file and so not in this; nothing a person could recover a secret from is.

A hash of content, not a version number, for the reason the page's own build
id gives in src/server/playstick/http.py: a number is something somebody has to
remember to raise. Compiled Python is left out because it is a by-product of
having run the code, not a change to it.

Standard library only: Ansible runs it with whatever python3 the control-node
container has.
"""

import hashlib
import os
import sys

# Relative to the repo. A directory means every file under it.
PARTS = (
    "site.yml",
    "inventory.yml",
    "ansible.cfg",
    "group_vars",
    "roles",
    "src/server/playstick",
    "src/server/playstick-web.py",
    "src/player/dist/playstick-ui.html",
)


def _files(repo):
    for part in PARTS:
        path = os.path.join(repo, part)
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                for name in sorted(filenames):
                    if not name.endswith(".pyc"):
                        full = os.path.join(dirpath, name)
                        yield os.path.relpath(full, repo).replace(os.sep, "/"), full
        else:
            yield part, path


def compute(repo, site=None):
    digest = hashlib.sha256()
    for rel, path in _files(repo):
        digest.update(rel.encode("utf-8") + b"\0")
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            # A file that should be there and is not -- an unbuilt page -- is
            # part of the answer rather than an error: the identity of a tree
            # without it.
            digest.update(b"\0missing\0")
        digest.update(b"\0")
    digest.update(b"site\0")
    if site:
        try:
            with open(site, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            pass
    return digest.hexdigest()[:12]


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__.strip().splitlines()[2].strip())
    print(compute(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None))
