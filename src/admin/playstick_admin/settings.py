"""Persistent application settings, backed by QSettings.

Everything a run needs that is not in the admin database: where the share is,
where films are prepared before they reach it, how to reach the stick, and the
optional TMDb key. Stored per-user by Qt in the platform's native location.

WHAT REPLACED `source_library` AND `output_dir`, AND WHY

There used to be one source directory and one output directory, and the output
defaulted to the source -- which was coherent while the only films this app
could see were the ones already on the share it wrote back to.

The host sees more than that. Sources are many and they live in the admin
database now, because a source is a thing with a scan history and a set of
films, not a string. What is left here is the one path that is genuinely
configuration rather than data: `nas_root`, the share as this machine mounts it.

`nas_root` is the frame of reference for everything sent to the stick. The stick
mounts the same share read-only at /srv/movies and resolves every path it is
given against that, refusing anything that lands outside. So every path in a
push is relative to this directory, and pointing it somewhere else invalidates
the whole database at once -- which is why core.identity.guard_nas_root exists
and why this module keeps the old keys around only long enough to import from.

There is no staging setting. A film is prepared in a dot folder on the share,
derived from `nas_root` by core.nas_store -- see that module for why it stopped
being configurable.
"""

import os

from PySide6.QtCore import QSettings

from . import paths

ORG = "playstick"
APP = "admin"

# Bumped when the keys below change shape, so migrate() runs once rather than
# on every launch. It is not a schema version -- QSettings has no schema -- it
# is a record of which one-way conversions have already happened.
SETTINGS_VERSION = 3


class Settings:
    def __init__(self):
        self._s = QSettings(ORG, APP)

    def _get(self, key, default=""):
        value = self._s.value(key, default)
        return value if value is not None else default

    def _set(self, key, value):
        self._s.setValue(key, value)
        self._s.sync()

    # --- the share ---------------------------------------------------------

    @property
    def nas_root(self):
        """The share, as it is mounted on THIS machine.

        Writable here and read-only on the stick, which is the asymmetry the
        whole arrangement rests on: this is the machine that puts films there.
        """
        return str(self._get("nas_root", ""))

    @nas_root.setter
    def nas_root(self, value):
        self._set("nas_root", value)

    def nas_root_problem(self):
        """Why this share cannot be used, or None.

        Checked at the point somebody sets it rather than at the point a job
        fails, because a read-only mount here produces a working scan, a working
        encode, and then a failure at the last step -- an hour later, every
        time. That is the same shape of invisible fault as the overrides file
        that lived on a read-only mount and silently lost every correction.
        """
        root = self.nas_root
        if not root:
            return "Set the NAS root: the share, as it is mounted on this machine."
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            return "%s is not a directory." % root
        if not os.access(root, os.W_OK):
            return ("%s is not writable. The stick mounts the share read-only, "
                    "but this machine is the one that puts films on it." % root)
        return None

    # --- the stick ---------------------------------------------------------

    @property
    def device_url(self):
        return str(self._get("device_url", "")).rstrip("/")

    @device_url.setter
    def device_url(self, value):
        self._set("device_url", value)

    # What Settings shows and takes: the stick's address -- an IP, or a name,
    # with a port when it is not 80 -- rather than a URL. The URL is still what
    # is stored, so everything that talks to the stick reads device_url as
    # before; this only spares a person typing the scheme.
    @property
    def device_ip(self):
        url = self.device_url
        for scheme in ("http://", "https://"):
            if url.startswith(scheme):
                return url[len(scheme):]
        return url

    @device_ip.setter
    def device_ip(self, value):
        value = value.strip().rstrip("/")
        if value and "://" not in value:
            value = "http://" + value
        self.device_url = value

    # How a deploy logs in to the stick: a user and, optionally, a private
    # key -- empty means whatever ssh would use by itself, the agent and the
    # default ~/.ssh/id_* files. The host is the device IP's. See
    # core.ssh_check for why there is no password.
    @property
    def ssh_user(self):
        return str(self._get("ssh_user", ""))

    @ssh_user.setter
    def ssh_user(self, value):
        self._set("ssh_user", value)

    @property
    def ssh_key(self):
        return str(self._get("ssh_key", ""))

    @ssh_key.setter
    def ssh_key(self, value):
        self._set("ssh_key", value)

    # --- the share, as the stick mounts it ---------------------------------
    #
    # Not the NAS root above, which is where THIS machine mounts it: the stick
    # mounts //<nas_server>/<nas_share> itself, read-only. Settings suggests
    # both from this machine's mount (core.nas_mount). They reach the playbook
    # through core.site_vars, and nowhere else sets them.

    @property
    def nas_server(self):
        return str(self._get("nas_server", ""))

    @nas_server.setter
    def nas_server(self, value):
        self._set("nas_server", value)

    @property
    def nas_share(self):
        return str(self._get("nas_share", ""))

    @nas_share.setter
    def nas_share(self, value):
        self._set("nas_share", value)

    # Optional; for the page's wake-on-LAN button.
    @property
    def nas_mac(self):
        return str(self._get("nas_mac", ""))

    @nas_mac.setter
    def nas_mac(self, value):
        self._set("nas_mac", value)

    # Empty mounts as guest. The password is not a setting -- it is in the
    # keyring, see core.credentials.
    @property
    def nas_username(self):
        return str(self._get("nas_username", ""))

    @nas_username.setter
    def nas_username(self, value):
        self._set("nas_username", value)

    # --- metadata ----------------------------------------------------------

    @property
    def tmdb_key(self):
        return str(self._get("tmdb_key", ""))

    @tmdb_key.setter
    def tmdb_key(self, value):
        self._set("tmdb_key", value)

    @property
    def tmdb_language(self):
        return str(self._get("tmdb_language", "en-US"))

    @tmdb_language.setter
    def tmdb_language(self, value):
        self._set("tmdb_language", value)

    # The playstick checkout the Config tab edits and runs actl in. Defaults
    # to the one this app is running from, or for an installed copy to what
    # the .deb put in /usr/share/playstick -- see paths.py.
    @property
    def repo_root(self):
        return str(self._get("repo_root", paths.REPO_ROOT))

    @repo_root.setter
    def repo_root(self, value):
        self._set("repo_root", value)

    # --- what the old settings said ----------------------------------------
    #
    # Read-only, and read by exactly one thing: core.import_legacy, which turns
    # a library prepared under the old arrangement into rows in the admin
    # database. Kept rather than deleted because the conversion needs to know
    # where to look, and a user who upgrades six months from now needs it as
    # much as one who upgrades today.

    @property
    def legacy_source_library(self):
        return str(self._get("source_library", ""))

    @property
    def legacy_output_dir(self):
        return str(self._get("output_dir", ""))

    def legacy_output(self):
        return self.legacy_output_dir or self.legacy_source_library

    def migrate(self):
        """One-way conversion of the old keys. Returns True if it did anything.

        Only the paths move here. The films, the corrections and the probe
        cache are a database import and belong in core.import_legacy, which
        this makes possible by working out which directory to import from.
        """
        if int(self._get("settings_version", 0) or 0) >= SETTINGS_VERSION:
            return False
        legacy = self.legacy_output()
        if legacy and not self.nas_root:
            # The old output directory IS the share: it is where the preparer
            # wrote and where the stick read. Defaulting to it is also what
            # keeps every existing film's id -- see core.identity.
            self.nas_root = legacy
        # Staging stopped being a setting, and so did the tools -- found by
        # tools.py now -- and the extra preparer flags. Left in the file, the
        # keys would read as though they still did something.
        for key in ("staging_dir", "ffmpeg", "ffprobe", "prep_extra_args"):
            self._s.remove(key)
        self._set("settings_version", SETTINGS_VERSION)
        return True
