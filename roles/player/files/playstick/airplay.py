"""The interlock that keeps mpv and UxPlay off each other's DRM master.

Two questions, deliberately asked differently: airplay_active() is one sample
and airplay_confirmed() is several, because only one caller is about to refuse
to do something. See the package docstring for why this arbitration lives in
the daemon rather than in systemd.
"""

import subprocess
import time

from .config import AIRPLAY_PORT, log


def airplay_active():
    """True while a client holds a TCP connection to UxPlay.

    The same question, asked the same way, as session_active() in
    uxplay-idle-clock.py -- and deliberately a second copy rather than a shared
    module, because the two callers do not want the same answer. The clock is
    driving a blank countdown and can afford to be wrong for one poll;
    airplay_confirmed() below cannot, so it debounces and the clock does not.
    Eight duplicated lines is a smaller thing than an import path shared
    between two units that then have to agree about sampling.

    Not 'is the DRM node open': uxplay-kms holds that for its whole lifetime
    and so can never distinguish idle from mirroring.
    """
    if not AIRPLAY_PORT:
        return False
    try:
        out = subprocess.run(
            ["ss", "-H", "-tn", "state", "established", "sport = :%s" % AIRPLAY_PORT],
            capture_output=True, text=True, timeout=5).stdout
        return bool(out.strip())
    except Exception:                                # noqa: BLE001
        return False


def airplay_confirmed(samples=2, interval=1.0):
    """Debounced, for the one caller that refuses to do something.

    iOS opens short-lived connections to UxPlay's port merely from having the
    AirPlay picker on screen -- nobody is mirroring, the list is just being
    drawn. A single ss sample would therefore refuse to start a film because
    somebody across the room glanced at a menu. Requiring the connection to
    survive consecutive samples distinguishes a session from a look.
    """
    for i in range(max(1, samples)):
        if not airplay_active():
            return False
        if i + 1 < samples:
            time.sleep(interval)
    return True


def systemctl(*args, timeout=45):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=timeout)
    except Exception as exc:                         # noqa: BLE001
        log("systemctl %s failed: %s", " ".join(args), exc)
        return None


def unit_active(unit):
    res = systemctl("is-active", "--quiet", unit, timeout=10)
    return bool(res) and res.returncode == 0
