"""Comment-preserving read/write of the Ansible variable files.

group_vars/all.yml is the appliance's configuration and it is almost entirely
comment -- every tunable is explained in place. Editing it must not throw that
away, so this uses ruamel.yaml in round-trip mode: untouched keys keep their
comments and their formatting, and only the values a human changed are rewritten.

ruamel is an optional import. Without it the config tab degrades to read-only
raw text rather than silently dropping every comment in the file.
"""

import io

try:
    from ruamel.yaml import YAML
    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.width = 4096
    HAVE_RUAMEL = True
except Exception:                       # pragma: no cover - environment dependent
    _yaml = None
    HAVE_RUAMEL = False


class ConfigError(Exception):
    pass


def load(path):
    """Return the parsed document (a round-trip map that remembers comments)."""
    if not HAVE_RUAMEL:
        raise ConfigError("ruamel.yaml is not installed; config editing is "
                          "read-only")
    with open(path, "r", encoding="utf-8") as handle:
        return _yaml.load(handle) or {}


def dump(data):
    """Serialise a document back to a string, comments and order intact."""
    if not HAVE_RUAMEL:
        raise ConfigError("ruamel.yaml is not installed")
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def save(path, data):
    text = dump(data)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# How each top-level key is grouped in the form, by prefix. Keys that match no
# prefix fall under "other", so a new variable still appears rather than hiding.
GROUPS = [
    ("Player / web daemon", ("player_",)),
    ("Projector", ("player_projector",)),
    ("UxPlay (AirPlay)", ("uxplay_",)),
    ("NAS mount", ("nas_",)),
    ("Graphics / DRM", ("drm_", "libva_", "i965_")),
    ("Idle clock", ("idle_",)),
    ("System trim", ("trim_",)),
    ("Probe / measurement", ("probe_",)),
    ("Base system", ("base_", "enable_", "claim_")),
]


def group_for(key):
    # Projector keys start with player_projector_, so check the longer prefix
    # first -- otherwise they all fall into the generic player group.
    for label, prefixes in sorted(GROUPS, key=lambda g: -max(len(p) for p in g[1])):
        if any(key.startswith(p) for p in prefixes):
            return label
    return "Other"
