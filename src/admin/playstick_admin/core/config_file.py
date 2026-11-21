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


# The projector variables are picked from a list rather than typed: a model the
# daemon does not know becomes no projector at all, and an input the projector
# does not have answers ER401, and both fail quietly. Kept here as well as on
# the device (src/server/playstick/projector/probe.py) so the form works with
# the stick switched off; a probe of the stick replaces nothing in these, it
# only picks from them and adds the serial ports.
PROJECTOR_MODEL_KEY = "player_projector_model"
PROJECTOR_DEVICE_KEY = "player_projector_device"
PROJECTOR_INPUT_KEY = "player_projector_input"

PROJECTOR_MODELS = (
    ("", "None -- no projector"),
    ("pt-ae4000", "Panasonic PT-AE4000"),
    ("pt-ae3000u", "Panasonic PT-AE3000U"),
)

PROJECTOR_DEVICES = (
    ("", "Find it -- the first USB serial adapter"),
)

PROJECTOR_INPUTS = (
    ("", "Leave the input alone"),
    ("HD1", "HDMI 1"),
    ("HD2", "HDMI 2"),
    ("HD3", "HDMI 3"),
    ("CP1", "Component 1"),
    ("CP2", "Component 2 (PT-AE3000U only)"),
    ("VID", "Composite video"),
    ("SVD", "S-video"),
    ("RG1", "Computer (PT-AE4000 only)"),
)

CHOICES = {
    PROJECTOR_MODEL_KEY: PROJECTOR_MODELS,
    PROJECTOR_DEVICE_KEY: PROJECTOR_DEVICES,
    PROJECTOR_INPUT_KEY: PROJECTOR_INPUTS,
}

# Typed into as well as picked from: a port the probe cannot see -- the stick
# switched off, a by-path link somebody prefers -- still has to be enterable.
EDITABLE_CHOICES = (PROJECTOR_DEVICE_KEY,)


def choices_for(key, current):
    """(value, label) pairs for a select-list key, with `current` kept.

    A value that is not in the table -- an older model name, a hand-edited
    device path -- is offered as its own entry rather than silently replaced by
    the first one, so opening the form and pressing Save changes nothing.
    """
    options = list(CHOICES[key])
    value = "" if current is None else str(current)
    if key == PROJECTOR_INPUT_KEY:
        value = value.upper()
    if value not in [v for v, _ in options]:
        options.append((value, value))
    return options
