"""Which NAS share a local folder is, read off this machine's own mount. Qt-free.

The stick mounts the share by server and share name; this machine has usually
mounted the same share already, at the NAS root Settings names. So when that
mount is CIFS, the server and share are in /proc/self/mountinfo and nobody has
to type them twice -- or get the share name wrong in the way the nas role's
assert exists to catch, the server-side /volume1/... path instead of the
exported name. The username this machine logged in with is in the mount
options too; the password is not, and is asked for.

Only a suggestion: Settings shows what was found and keeps whatever is typed.
An NFS mount names a server-side path, not a share, so it suggests nothing.
"""

import os

MOUNTINFO = "/proc/self/mountinfo"
CIFS = ("cifs", "smb3")


def _unescape(field):
    """mountinfo writes a space as \\040, a tab as \\011 and so on."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def detect(path, mountinfo=None):
    """(server, share, username) for the CIFS mount `path` is on, or None.
    The username is "" for a guest mount.

    `mountinfo` is the file's text, for the tests; read from /proc otherwise.
    """
    if not path:
        return None
    if mountinfo is None:
        try:
            with open(MOUNTINFO, encoding="utf-8") as handle:
                mountinfo = handle.read()
        except OSError:
            return None
    target = os.path.realpath(path)
    best = None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        point = _unescape(fields[4])
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        # The innermost mount wins: / contains everything.
        if inside and (best is None or len(point) > len(best[0])):
            best = (point, tail[0], _unescape(tail[1]),
                    tail[2] if len(tail) > 2 else "")
    if best is None or best[1] not in CIFS:
        return None
    parts = best[2].replace("\\", "/").strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    username = ""
    for option in best[3].split(","):
        if option.startswith(("username=", "user=")):
            username = option.split("=", 1)[1]
    return parts[0], parts[1], username
