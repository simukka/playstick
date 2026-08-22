"""The settings loader: what happens when the file is wrong.

config.py is read once at import and cannot be re-imported per test, so these
exercise the helpers against dicts they are handed rather than against the
module's own SETTINGS. That is the whole surface -- _load turns a path into a
dict, _get/_flag/_words turn a dict into a value -- and testing them separately
is what makes the "never raises" claim checkable at all.

The claim matters more than it looks. A daemon that refuses to start because a
bad Ansible run left a stray bracket in its configuration is a daemon that
turns a typo into a dark projector, on an appliance with no console to say so.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "server"))
sys.dont_write_bytecode = True

from playstick import config                                      # noqa: E402


class Load(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="playstick-settings-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def write(self, text):
        path = os.path.join(self.dir, "settings.toml")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_a_missing_file_is_the_developer_machine_not_an_error(self):
        """The path taken by `python3 src/server/playstick-web.py`, which is
        supposed to come up on 8080 with the in-code defaults."""
        self.assertEqual(config._load(os.path.join(self.dir, "nope.toml")), {})

    def test_a_readable_file_is_parsed(self):
        path = self.write('[server]\nport = 8081\nbind = "127.0.0.1"\n')
        self.assertEqual(config._load(path),
                         {"server": {"port": 8081, "bind": "127.0.0.1"}})

    def test_a_broken_file_falls_back_rather_than_raising(self):
        """The one that would otherwise take the appliance down. A stray
        bracket must cost the defaults, not the service."""
        path = self.write('[server\nport = 8081\n')
        self.assertEqual(config._load(path), {})

    def test_a_directory_where_a_file_belongs_falls_back(self):
        self.assertEqual(config._load(self.dir), {})

    def test_the_real_template_renders_to_something_this_can_read(self):
        """Not a unit test of anything in the daemon: it is the join between
        settings.toml.j2 and this loader, and the only place the two are
        checked against each other without a device."""
        path = self.write(
            '[server]\nport = 80\nallow_networks = ["10.0.0.0/8"]\n'
            '[library]\nskip_pattern = "sample|extras?\\\\b"\n'
            '[thumbs]\nat = "20%"\n'
            '[mpv]\nargs = ["--vf=scale=320:-2", "--ao=null"]\n')
        data = config._load(path)
        # The three values that had to be escaped by hand in the unit file it
        # replaced: a percent systemd read as a specifier, a regex full of
        # backslashes, and an argv that was one space-joined string.
        self.assertEqual(data["thumbs"]["at"], "20%")
        self.assertEqual(data["library"]["skip_pattern"], r"sample|extras?\b")
        self.assertEqual(data["mpv"]["args"], ["--vf=scale=320:-2", "--ao=null"])


class Accessors(unittest.TestCase):
    """_get, _flag and _words against a SETTINGS that is deliberately partial,
    which is the normal case: the template renders every key, but a hand-
    written file and the test harness both write only what they care about."""

    def setUp(self):
        self.saved = config.SETTINGS
        self.addCleanup(setattr, config, "SETTINGS", self.saved)

    def use(self, data):
        config.SETTINGS = data

    def test_a_present_value_wins(self):
        self.use({"server": {"port": 8081}})
        self.assertEqual(config._get("server", "port", 80), 8081)

    def test_a_missing_key_takes_the_default(self):
        self.use({"server": {}})
        self.assertEqual(config._get("server", "port", 80), 80)

    def test_a_missing_section_takes_the_default(self):
        self.use({})
        self.assertEqual(config._get("server", "port", 80), 80)

    def test_a_section_of_the_wrong_shape_takes_the_default(self):
        """A hand-edit that wrote `server = "yes"` should cost that section,
        not an AttributeError three imports away."""
        self.use({"server": "yes"})
        self.assertEqual(config._get("server", "port", 80), 80)

    def test_an_explicit_empty_string_is_a_value_and_not_a_default(self):
        """The load-bearing one. index_file = "" means "ignore any index that
        is present", which is a different instruction from the key being
        absent -- that means "the usual place, beside the films". The template
        always renders the key, so this is the form the daemon actually sees.
        """
        self.use({"library": {"index_file": ""}})
        self.assertEqual(config._get("library", "index_file", "sentinel"), "")

    def test_flags_accept_real_booleans_and_the_hand_written_spellings(self):
        for written, expected in [(True, True), (False, False),
                                  ("yes", True), ("no", False),
                                  ("true", True), ("1", True), ("0", False),
                                  (1, True), (0, False)]:
            self.use({"display": {"has_audio": written}})
            self.assertIs(config._flag("display", "has_audio", False), expected,
                          "has_audio = %r" % (written,))

    def test_words_take_a_list_or_the_space_separated_string(self):
        self.use({"library": {"extensions": [".mkv", ".mp4"]}})
        self.assertEqual(config._words("library", "extensions", []), [".mkv", ".mp4"])
        self.use({"library": {"extensions": ".mkv .mp4"}})
        self.assertEqual(config._words("library", "extensions", []), [".mkv", ".mp4"])

    def test_networks_parse_from_a_list_and_skip_what_is_not_one(self):
        """Unparseable entries are dropped with a line rather than failing the
        import -- same judgement as _load, for the same reason."""
        nets = config._parse_networks(["10.0.0.0/8", "not-a-network", "127.0.0.1"])
        self.assertEqual([str(n) for n in nets], ["10.0.0.0/8", "127.0.0.1/32"])


if __name__ == "__main__":
    unittest.main()
