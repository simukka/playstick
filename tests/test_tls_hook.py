"""The certbot DNS hooks in roles/tls/files, run against a fake dig.

The auth hook is the whole of the "any DNS provider" design -- it writes down
the record a person has to add, and decides when it has been added -- so it is
tested as a program rather than trusted as a script. The fake dig answers NS
for the zone and TXT from a file the test writes, which stands in for the
person at the DNS provider's form.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH = os.path.join(ROOT, "roles", "tls", "files", "playstick-tls-auth-hook")
DEPLOY = os.path.join(ROOT, "roles", "tls", "files", "playstick-tls-deploy-hook")

FAKE_DIG = """#!/bin/sh
# NS answers only for the zone apex, as real DNS does for a name with an A
# record. TXT answers come from $FAKE_TXT, and only when asked @a server --
# so a hook that asked its own resolver would never see the record.
qtype="" name="" server=""
for arg in "$@"; do
    case $arg in
        @*) server=${arg#@} ;;
        +*) ;;
        NS|TXT) qtype=$arg ;;
        *) name=$arg ;;
    esac
done
echo "$qtype $name $server" >> "$FAKE_LOG"
if [ "$qtype" = NS ] && [ "$name" = example.com ]; then
    echo ns1.example.net.
    echo ns2.example.net.
elif [ "$qtype" = TXT ] && [ -n "$server" ] && [ -f "$FAKE_TXT" ]; then
    grep -q "^$server " "$FAKE_TXT.servers" 2>/dev/null || exit 0
    printf '"%s"\\n' "$(cat "$FAKE_TXT")"
fi
"""


class AuthHook(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="playstick-hook-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.state = os.path.join(self.dir, "state")
        self.dig = os.path.join(self.dir, "dig")
        with open(self.dig, "w") as handle:
            handle.write(FAKE_DIG)
        os.chmod(self.dig, os.stat(self.dig).st_mode | stat.S_IEXEC)
        self.txt = os.path.join(self.dir, "txt")
        self.log = os.path.join(self.dir, "log")

    def publish(self, value, servers=("ns1.example.net.", "ns2.example.net.")):
        with open(self.txt, "w") as handle:
            handle.write(value)
        with open(self.txt + ".servers", "w") as handle:
            handle.writelines("%s \n" % s for s in servers)

    def run_hook(self, wait=2, value="tok-123_ABC", background=False):
        env = dict(os.environ, CERTBOT_DOMAIN="playstick.example.com",
                   CERTBOT_VALIDATION=value, PLAYSTICK_TLS_STATE=self.state,
                   PLAYSTICK_TLS_WAIT=str(wait), PLAYSTICK_TLS_INTERVAL="0.1",
                   PLAYSTICK_TLS_DIG=self.dig, FAKE_TXT=self.txt,
                   FAKE_LOG=self.log)
        args = ["sh", AUTH]
        if background:
            return subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        return subprocess.run(args, env=env, capture_output=True, timeout=30)

    def pending(self):
        with open(os.path.join(self.state, "pending.json")) as handle:
            return json.load(handle)

    def test_it_writes_down_the_record_with_its_zone(self):
        self.publish("tok-123_ABC")
        self.assertEqual(self.run_hook().returncode, 0)
        got = self.pending()
        self.assertEqual(got["record"], "_acme-challenge.playstick.example.com")
        self.assertEqual(got["value"], "tok-123_ABC")
        self.assertEqual(got["zone"], "example.com")
        self.assertEqual(got["wait"], 2)

    def test_it_waits_until_the_record_appears(self):
        proc = self.run_hook(wait=10, background=True)
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "returned before the record existed")
        self.publish("tok-123_ABC")
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_it_gives_up_and_keeps_the_record_for_the_app(self):
        result = self.run_hook(wait=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"did not appear", result.stderr)
        self.assertEqual(self.pending()["value"], "tok-123_ABC")

    def test_one_name_server_is_not_enough(self):
        self.publish("tok-123_ABC", servers=("ns1.example.net.",))
        self.assertEqual(self.run_hook(wait=1).returncode, 1)

    def test_a_stale_value_is_not_the_new_one(self):
        self.publish("an-older-token")
        self.assertEqual(self.run_hook(wait=1).returncode, 1)

    def test_it_asks_the_name_servers_not_the_local_resolver(self):
        self.publish("tok-123_ABC")
        self.run_hook()
        with open(self.log) as handle:
            txt = [line.split() for line in handle if line.startswith("TXT")]
        self.assertTrue(txt)
        self.assertTrue(all(len(q) == 3 for q in txt), txt)

    def test_the_deploy_hook_forgets_the_record(self):
        self.publish("tok-123_ABC")
        self.run_hook()
        subprocess.run(["sh", DEPLOY], check=True,
                       env=dict(os.environ, PLAYSTICK_TLS_STATE=self.state))
        self.assertFalse(os.path.exists(os.path.join(self.state,
                                                     "pending.json")))


if __name__ == "__main__":
    unittest.main()
