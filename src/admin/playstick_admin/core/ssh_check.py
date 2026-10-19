"""Can this machine log in to the stick the way a deploy will? Qt-free.

A deploy is Ansible over ssh with nobody at the keyboard: a key, never a typed
password, and a host key already in known_hosts, because ansible.cfg has
host_key_checking on. So the test is `ssh -o BatchMode=yes ... true` and
nothing friendlier. A check that prompted for a password, or accepted an
unknown host key on the way, would pass on the very setups the first deploy
then fails on -- and that failure arrives inside a container, as an Ansible
traceback, minutes later.

What it says when it fails is the point of it. ssh's last line is accurate and
rarely tells a person what to do next; `explain` turns the handful that come up
into the step that fixes each.
"""

import os
import subprocess

TIMEOUT = 15


def host_of(device_ip):
    """The host part of what Settings calls the device IP: "10.0.1.5:8080" is
    the web port, and ssh wants 10.0.1.5. A bracketed IPv6 address keeps its
    colons."""
    value = (device_ip or "").strip()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value[1:]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def argv(host, user, key="", binary="ssh"):
    out = [binary, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=yes"]
    if key:
        out += ["-i", os.path.expanduser(key), "-o", "IdentitiesOnly=yes"]
    out += ["%s@%s" % (user, host), "true"]
    return out


def explain(stderr, host, user):
    """The step that fixes it, for the failures that come up."""
    text = stderr.strip()
    last = text.splitlines()[-1] if text else "ssh failed"
    # Before the next test: a changed key also ends "Host key verification
    # failed", and the fix for it is a different command.
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text:
        return ("The stick's host key has changed since this machine last saw "
                "it -- expected after a reinstall. If that is what happened:\n"
                "  ssh-keygen -R %s" % host)
    if ("Host key verification failed" in text
            or ("host key" in text and "is known" in text)):
        return ("The stick's host key is not in known_hosts yet. Log in once "
                "from a terminal to check and accept it:\n  ssh %s@%s"
                % (user, host))
    if "Permission denied" in text:
        return ("The stick refused the key. Copy yours across, then test "
                "again:\n  ssh-copy-id %s@%s" % (user, host))
    if "Could not resolve hostname" in text:
        return "%s does not resolve from this machine." % host
    if "timed out" in text or "No route to host" in text:
        return "%s did not answer. Is the stick on, and on this network?" % host
    if "Connection refused" in text:
        return "%s answered, but nothing is listening for ssh on it." % host
    if "no such identity" in text or "Load key" in text:
        return "The key file could not be read: %s" % last
    return last


def test(host, user, key="", binary="ssh"):
    """Returns (ok, sentence)."""
    if not host:
        return False, "Set the device IP first."
    if not user:
        return False, "Set the user to log in as."
    try:
        res = subprocess.run(argv(host, user, key, binary),
                             capture_output=True, text=True, timeout=TIMEOUT,
                             stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, "ssh is not installed on this machine."
    except subprocess.TimeoutExpired:
        return False, "%s did not answer within %ds." % (host, TIMEOUT)
    if res.returncode == 0:
        return True, "Logged in to %s as %s." % (host, user)
    return False, explain(res.stderr, host, user)
