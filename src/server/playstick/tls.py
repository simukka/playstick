"""HTTPS beside HTTP, for the browser features that refuse a plain page.

The phone page is served on plain HTTP and that stays exactly as it is. This is
a second listener with the same handler, and it exists for one reason: browsers
keep some APIs -- the motion sensors above all -- for secure contexts, and a
page at http://10.0.1.x can never be one. Nothing here is about secrecy on the
LAN, which is the same LAN the allowlist already trusts.

EVERY WAY THIS CAN FAIL LEAVES HTTP UP

No certificate yet (the first deploy has not issued one), a key that does not
match, a file certbot is halfway through replacing: each is logged once and the
daemon carries on serving HTTP. It is the judgement projector/__init__.py makes
about a serial cable -- a movie player that can also do HTTPS, not an HTTPS
server that can also play films.

A RENEWAL DOES NOT RESTART ANYTHING

certbot replaces the files every sixty days or so, from its own timer. A deploy
hook that restarted the daemon would stop whatever film was on, because
stopping the daemon stops mpv. Instead CertWatcher looks at the files when a
connection arrives -- at most once a minute -- and builds a fresh SSLContext if
they changed. Connections already open keep the context they were accepted
with, which is the right answer for a keep-alive phone mid-poll.

THE HANDSHAKE IS NOT DONE HERE

get_request() runs in the server's accept loop, and a handshake there would let
one client that connects and says nothing stall every other phone. The socket
is wrapped with do_handshake_on_connect=False and Handler.setup() completes the
handshake on the request's own thread, under a timeout -- see http.py.

THE CERTIFICATE MAY ARRIVE LATER

On a first deploy there is no certificate when the daemon starts: the tls
role's unit is still waiting for a person to add a TXT record. Listener keeps
trying, once a minute, and HTTPS comes up when the files appear -- no restart.
status() is what the admin app shows meanwhile: the record wanted, whether an
attempt is waiting for it, and later the certificate's expiry.

Standard library only, like the rest of the daemon.
"""

import json
import os
import ssl
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

from .config import log


# How often a connection may cause the files to be looked at. A renewal is not
# urgent -- the old certificate is valid for weeks after the new one exists --
# and a stat per connection per phone per poll would be pointless churn.
CHECK_SECONDS = 60.0


class CertWatcher:
    """The current SSLContext for a certificate and key on disk."""

    def __init__(self, cert, key, clock=time.monotonic, interval=CHECK_SECONDS):
        self._cert = cert
        self._key = key
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._context = None
        self._stamp = None
        self._checked = None

    def _stamp_now(self):
        # Through the symlinks: certbot's live/ files point into archive/, and
        # a renewal moves the links, which changes the inode they lead to.
        stamps = []
        for path in (self._cert, self._key):
            st = os.stat(path)
            stamps.append((st.st_ino, st.st_mtime_ns, st.st_size))
        return tuple(stamps)

    def _build(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self._cert, self._key)
        return context

    def context(self):
        """The context to wrap the next connection with.

        Raises only while there has never been a good one. After that, a file
        that will not read -- certbot between writing the certificate and the
        key, say -- keeps the last good context and is tried again on a later
        check.
        """
        now = self._clock()
        with self._lock:
            if (self._context is not None and self._checked is not None
                    and now - self._checked < self._interval):
                return self._context
            self._checked = now
            try:
                stamp = self._stamp_now()
                if stamp == self._stamp:
                    return self._context
                context = self._build()
            except (OSError, ssl.SSLError) as exc:
                if self._context is None:
                    raise
                log("tls: could not reload %s: %s; keeping the certificate "
                    "already loaded", self._cert, exc)
                return self._context
            if self._context is not None:
                log("tls: certificate %s reloaded", self._cert)
            self._context = context
            self._stamp = stamp
            return context


class TlsServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer whose accepted sockets are wrapped in TLS."""

    def __init__(self, address, handler, watcher):
        self.watcher = watcher
        super().__init__(address, handler)

    def get_request(self):
        sock, addr = super().get_request()
        try:
            wrapped = self.watcher.context().wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False)
        except (OSError, ssl.SSLError):
            # serve_forever treats an OSError from get_request as "no request
            # this time" and carries on, which is exactly the intent.
            sock.close()
            raise OSError("no usable certificate")
        return wrapped, addr


def _try_open(bind, port, cert, key, handler, clock=time.monotonic):
    """(server, "") or (None, why). Nothing is logged here."""
    watcher = CertWatcher(cert, key, clock=clock)
    try:
        watcher.context()
    except (OSError, ssl.SSLError) as exc:
        return None, "not serving HTTPS on %d: %s" % (port, exc)
    try:
        server = TlsServer((bind, port), handler, watcher)
    except OSError as exc:
        return None, "could not listen on %s:%d: %s" % (bind, port, exc)
    server.daemon_threads = True
    return server, ""


def open_server(bind, port, cert, key, handler, clock=time.monotonic):
    """A TlsServer, or None when HTTPS is off or cannot start.

    The certificate is loaded here, once, so that "the key does not match" is
    a line in the journal rather than a silent refusal of every phone that
    tries.
    """
    if not (port and cert and key):
        return None
    server, why = _try_open(bind, port, cert, key, handler, clock)
    if server is None:
        log("tls: %s", why)
    return server


# How often Listener looks for a certificate that is not there yet.
RETRY_SECONDS = 60.0


class Listener:
    """The HTTPS server, started whenever its certificate first works."""

    def __init__(self, bind, port, cert, key, handler, retry=RETRY_SECONDS):
        self._args = (bind, port, cert, key, handler)
        self._retry = retry
        self._lock = threading.Lock()
        self._server = None

    def run(self, stopping):
        bind, port, cert, key, handler = self._args
        if not (port and cert and key):
            return
        said = None
        while not stopping.is_set():
            server, why = _try_open(*self._args)
            if server is not None:
                with self._lock:
                    if stopping.is_set():
                        server.server_close()
                        return
                    self._server = server
                log("listening on %s:%d (https)", bind, port)
                try:
                    server.serve_forever(poll_interval=0.5)
                finally:
                    server.server_close()
                return
            # Once per distinct reason, not once a minute: a certificate that
            # is waiting on a DNS record can be missing for days.
            if why != said:
                log("tls: %s; trying again every %ds", why, int(self._retry))
                said = why
            stopping.wait(self._retry)

    def shutdown(self):
        with self._lock:
            server = self._server
        if server is not None:
            server.shutdown()


# -- what the admin app shows


def _certificate(cert):
    """(expiry as epoch seconds, issuer organisation) or (None, "")."""
    try:
        # Private, but present in every CPython since 3.4, and the only way the
        # standard library reads a certificate file without a handshake.
        decoded = ssl._ssl._test_decode_cert(cert)
    except (OSError, ssl.SSLError, ValueError, AttributeError):
        return None, ""
    try:
        expires = ssl.cert_time_to_seconds(decoded["notAfter"])
    except (KeyError, ValueError):
        expires = None
    fields = {key: value for rdn in decoded.get("issuer", ()) for key, value in rdn}
    return expires, fields.get("commonName") or fields.get("organizationName", "")


def _pending(state_dir):
    try:
        with open(os.path.join(state_dir, "pending.json"), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("record") or not data.get("value"):
        return None
    return {"record": str(data["record"]), "value": str(data["value"]),
            "domain": str(data.get("domain", "")),
            "zone": str(data.get("zone", "")),
            "since": data.get("since"), "wait": data.get("wait")}


# How much of an attempt's journal the app is shown. An attempt that waited a
# quarter of an hour writes one line when it starts and one when it ends; the
# rest is certbot, which is brief.
LOG_LINES = 40
LOG_LINE_CHARS = 300

# What certbot and the auth hook say, and what it means to somebody who pressed
# a button. First match wins, so the most specific lines come first.
_OUTCOMES = (
    ("Successfully received certificate", "issued"),
    ("Certificate not yet due for renewal", "not_due"),
    ("did not appear within", "no_record"),
    ("is published on every name server", "record_seen"),
    ("too many certificates", "rate_limited"),
    ("Some challenges have failed", "challenge_failed"),
)


def _unix(value):
    """systemctl show --timestamp=unix gives "@1795280486", or "" if never."""
    value = (value or "").strip()
    if value.startswith("@"):
        try:
            return int(value[1:])
        except ValueError:
            return None
    return None


def unit_report(unit, run=subprocess.run):
    """The last certificate attempt: whether it is running, how it ended, when,
    and what it wrote to the journal -- for the Device page to say whether
    "Check now" worked, rather than leave somebody guessing.

    Empty dict when systemctl cannot be asked (a development machine), which
    the app reads as "no report" rather than as a failure.
    """
    if not unit:
        return {}
    try:
        shown = run(["systemctl", "show", "--timestamp=unix",
                     "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus",
                     "-p", "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp",
                     "-p", "InvocationID", unit],
                    capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if shown.returncode != 0:
        return {}
    props = dict(line.split("=", 1) for line in shown.stdout.splitlines()
                 if "=" in line)
    invocation = props.get("InvocationID", "").strip()
    lines = []
    if invocation:
        try:
            # Both fields: the first is what certbot and the hook printed, the
            # second what systemd said about the attempt -- "Finished", or
            # "Failed with result 'exit-code'", which is the line that matters.
            journal = run(["journalctl", "--no-pager", "-o", "cat",
                           "-n", str(LOG_LINES),
                           "_SYSTEMD_INVOCATION_ID=" + invocation, "+",
                           "INVOCATION_ID=" + invocation],
                          capture_output=True, text=True, timeout=10)
            if journal.returncode == 0:
                lines = [line[:LOG_LINE_CHARS]
                         for line in journal.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    outcome = ""
    for needle, word in _OUTCOMES:
        if any(needle in line for line in lines):
            outcome = word
            break
    try:
        exit_status = int(props.get("ExecMainStatus", "") or 0)
    except ValueError:
        exit_status = None
    started = _unix(props.get("ExecMainStartTimestamp"))
    return {
        "active": props.get("ActiveState", ""),
        "result": props.get("Result", ""),
        "exit_status": exit_status,
        "started": started,
        "finished": _unix(props.get("ExecMainExitTimestamp")),
        # Never run since boot: systemd has no start time to give.
        "ran": started is not None,
        "outcome": outcome,
        "log": lines,
    }


def status(name, cert, state_dir, unit=lambda: {}, now=time.time):
    """Everything the Device page says about HTTPS.

    `state` is the one word the page decides on:
      off        no name: HTTPS is not wanted
      valid      a certificate is in place and nothing is being asked for
      waiting    an attempt is watching the name servers for `pending`
      record     the last attempt gave up waiting for `pending`
      checking   an attempt is running and has not asked for anything yet
      missing    no certificate, nothing running, nothing asked for

    waiting and record happen with a certificate in place as well, when a
    renewal wants a new value -- `certificate` and `expires` still say what is
    being served meanwhile.
    """
    if not name:
        return {"enabled": False, "state": "off"}
    expires, issuer = _certificate(cert) if cert else (None, "")
    pending = _pending(state_dir) if state_dir else None
    report = unit() or {}
    running = report.get("active") == "activating"
    if expires is not None and not pending:
        state = "valid"
    elif pending:
        state = "waiting" if running else "record"
    elif running:
        state = "checking"
    else:
        state = "missing"
    return {
        "enabled": True,
        "state": state,
        "name": name,
        "certificate": expires is not None,
        "expires": expires,
        "issuer": issuer,
        "staging": "STAGING" in issuer.upper() or "FAKE" in issuer.upper(),
        "running": running,
        "pending": pending,
        # The last attempt, for "Check now" to report on. See unit_report.
        "last": report,
        "now": now(),
    }
