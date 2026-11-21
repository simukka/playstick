"""What a deploy needs to know about this site, handed to Ansible by actl.

It used to be host_vars/vivostick/local.yml in the checkout, filled in by hand
from a template, plus an ansible-vault file for the NAS password. Neither works
for an installed app: the checkout it deploys from is /usr/share/playstick and
belongs to root. So the app is the one place these are set, and this writes
them where actl can find them:

    <state>/inventory/site.yml      the stick's address and login, the NAS
    <state>/inventory/secrets.yml   the NAS password, and the stick's sudo
                                    password for an update; only while the
                                    app is running actl, then deleted
    <state>/ssh_key                 a link to the key Settings names

actl mounts the directory into the control-node container, and ansible.cfg
lists it as a second inventory source beside inventory.yml. Host variables from
an inventory outrank group_vars/all.yml, so the values here win over its empty
defaults -- the same relationship local.yml had.

site.yml is rewritten whenever Settings is saved and before every actl run, so
running actl from a terminal still reaches the stick. The password is not in it:
it lives in the keyring (see credentials.py), and a terminal run that needs it
is stopped by the nas role rather than writing an empty one onto the stick.

Both files are JSON, which is YAML, so nothing here can mis-quote a value.

APPLIANCE OVERRIDES

The Config tab's Advanced form edits group_vars/all.yml in place when it can.
The installed copy belongs to root, so it cannot -- and then what the form saves
is kept in <state>/appliance.json and merged into site.yml under the Settings
values. The same precedence that lets site.yml fill all.yml's empty NAS values
makes these win over all.yml's defaults, and because they are in site.yml they
are part of the release id too: changing the projector model from an installed
app is an update the Device tab offers, exactly as if all.yml had changed.
"""

import json
import os

from .. import paths
from . import ssh_check

HOST = "vivostick"

# Where actl mounts things inside the container. Fixed, so site.yml can name
# the key by a path that means something there.
CONTAINER_KEY = "/run/playstick/ssh_key"


def inventory_dir():
    return os.path.join(paths.state_dir(), "inventory")


def site_path():
    return os.path.join(inventory_dir(), "site.yml")


def secrets_path():
    return os.path.join(inventory_dir(), "secrets.yml")


def overrides_path():
    return os.path.join(paths.state_dir(), "appliance.json")


def load_overrides():
    """Appliance variables saved by an app that could not write all.yml, or
    {}. A file that will not parse is treated as empty rather than fatal: the
    worst case is a deploy with all.yml's own values, which is a known state."""
    try:
        with open(overrides_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_overrides(values):
    """Replace them. Empty removes the file, so a machine that has gone back
    to editing a checkout carries nothing over from the installed copy."""
    path = overrides_path()
    if not values:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def key_link():
    return os.path.join(paths.state_dir(), "ssh_key")


def host_vars(settings):
    """Every site value, from Settings. Empty NAS values are written as empty
    rather than left out: left out, a value in group_vars/all.yml would apply
    instead, and these are meant to be the only source."""
    out = {}
    host = ssh_check.host_of(settings.device_ip)
    if host:
        out["ansible_host"] = host
    if settings.ssh_user:
        out["ansible_user"] = settings.ssh_user
    if settings.ssh_key:
        out["ansible_ssh_private_key_file"] = CONTAINER_KEY
    out["nas_server"] = settings.nas_server
    out["nas_share"] = settings.nas_share
    out["nas_mac"] = settings.nas_mac
    out["nas_username"] = settings.nas_username
    return out


def _write(path, variables):
    """Atomically and private. The temporary name starts with a dot because
    Ansible skips dot files in an inventory directory, so a half-written one
    is never read."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = os.path.join(os.path.dirname(path),
                       ".%s.part" % os.path.basename(path))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# Written by the playstick app from its Settings; "
                     "changes here are overwritten.\n")
        json.dump({"all": {"hosts": {HOST: variables}}}, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def write_site(settings):
    # Settings last: a site value is never something the Advanced form owns,
    # and an override of one would be a stale leftover rather than intent.
    variables = dict(load_overrides())
    variables.update(host_vars(settings))
    _write(site_path(), variables)
    _link_key(settings.ssh_key)


def _link_key(key):
    link = key_link()
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    if key:
        os.makedirs(os.path.dirname(link), mode=0o700, exist_ok=True)
        os.symlink(os.path.abspath(os.path.expanduser(key)), link)


def write_secrets(**variables):
    """nas_password, ansible_become_password: whichever this run has. An
    empty one is left out rather than written as "", which would override a
    sudo that needs no password with a wrong one."""
    _write(secrets_path(), {k: v for k, v in variables.items() if v})


def clear_secrets():
    """After every actl run, and at startup in case the last run never got to
    its own clear-up."""
    try:
        os.unlink(secrets_path())
    except FileNotFoundError:
        pass
