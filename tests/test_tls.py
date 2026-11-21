"""The HTTPS listener: that it serves the same page, survives a renewal without
a restart, and cannot be the reason the daemon -- or another phone -- stops.

Certificates are made per test with the openssl binary, because the standard
library can use a certificate but cannot make one. The suite is skipped where
there is no openssl rather than failing, the same way a missing ffmpeg is
handled elsewhere.
"""

import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from support import ApiTest, Clock, api
from playstick import tls


HAVE_OPENSSL = shutil.which("openssl") is not None


def make_cert(directory, name):
    cert = os.path.join(directory, name + ".crt")
    key = os.path.join(directory, name + ".key")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec",
         "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", key, "-out", cert],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl binary")
class Https(ApiTest):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="playstick-tls-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cert, self.key = make_cert(self.dir, "live")
        self.clock = Clock()

    def start(self):
        # Built from the parts rather than through open_server, because port 0
        # -- "any free port", which a test needs -- is "off" to open_server.
        watcher = tls.CertWatcher(self.cert, self.key, clock=self.clock)
        watcher.context()
        server = tls.TlsServer(("127.0.0.1", 0), self.server.RequestHandlerClass,
                               watcher)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def client(self, port):
        context = ssl.create_default_context(cafile=None)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection("127.0.0.1", port, timeout=5,
                                           context=context)

    def peer_cert(self, port):
        conn = self.client(port)
        conn.request("GET", "/healthz")
        conn.getresponse().read()
        der = conn.sock.getpeercert(binary_form=True)
        conn.close()
        return der

    def test_the_same_routes_answer_over_https(self):
        port = self.start()
        conn = self.client(port)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b'"state"', resp.read())
        conn.close()

    def test_a_renewed_certificate_is_served_without_a_restart(self):
        port = self.start()
        before = self.peer_cert(port)
        new_cert, new_key = make_cert(self.dir, "renewed")
        os.replace(new_key, self.key)
        os.replace(new_cert, self.cert)
        # Within the check interval the old one is still served...
        self.assertEqual(self.peer_cert(port), before)
        # ...and after it, the new one, with nothing restarted.
        self.clock.advance(tls.CHECK_SECONDS + 1)
        self.assertNotEqual(self.peer_cert(port), before)

    def test_a_half_written_renewal_keeps_the_old_certificate(self):
        port = self.start()
        before = self.peer_cert(port)
        other_cert, _other_key = make_cert(self.dir, "other")
        # Certificate replaced, key not yet: they no longer match.
        os.replace(other_cert, self.cert)
        self.clock.advance(tls.CHECK_SECONDS + 1)
        self.assertEqual(self.peer_cert(port), before)

    def test_a_client_that_never_handshakes_does_not_block_another(self):
        port = self.start()
        silent = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(silent.close)
        started = time.monotonic()
        conn = self.client(port)
        conn.request("GET", "/healthz")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertLess(time.monotonic() - started, 3)

    def test_plain_http_is_untouched(self):
        self.start()
        self.assertEqual(self.fetch("/healthz").status, 200)


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl binary")
class Startup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="playstick-tls-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_off_unless_all_three_are_set(self):
        cert, key = make_cert(self.dir, "a")
        for args in ((0, cert, key), (8443, "", key), (8443, cert, "")):
            with self.subTest(args=args):
                self.assertIsNone(tls.open_server("127.0.0.1", *args, None))

    def test_a_missing_certificate_is_no_server_and_no_exception(self):
        missing = os.path.join(self.dir, "nope.pem")
        self.assertIsNone(tls.open_server("127.0.0.1", 8443, missing, missing,
                                          None))

    def test_a_key_that_does_not_match_is_no_server(self):
        cert, _key = make_cert(self.dir, "a")
        _cert, other_key = make_cert(self.dir, "b")
        self.assertIsNone(tls.open_server("127.0.0.1", 8443, cert, other_key,
                                          None))


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl binary")
class LateCertificate(ApiTest):
    """A first deploy: the daemon starts before the certificate exists."""

    def test_https_comes_up_when_the_files_appear(self):
        directory = tempfile.mkdtemp(prefix="playstick-tls-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        cert = os.path.join(directory, "fullchain.pem")
        key = os.path.join(directory, "privkey.pem")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        listener = tls.Listener("127.0.0.1", port, cert, key,
                                self.server.RequestHandlerClass, retry=0.05)
        stopping = threading.Event()
        thread = threading.Thread(target=listener.run, args=(stopping,),
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(listener.shutdown)
        self.addCleanup(stopping.set)
        time.sleep(0.2)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        made_cert, made_key = make_cert(directory, "issued")
        os.replace(made_key, key)
        os.replace(made_cert, cert)
        deadline = time.monotonic() + 5
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)


class Status(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="playstick-tls-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cert = os.path.join(self.dir, "fullchain.pem")

    def pending(self, **extra):
        data = {"domain": "playstick.example.com", "zone": "example.com",
                "record": "_acme-challenge.playstick.example.com",
                "value": "abc123", "since": 1000, "wait": 900}
        data.update(extra)
        with open(os.path.join(self.dir, "pending.json"), "w") as handle:
            json.dump(data, handle)

    def status(self, unit=""):
        return tls.status("playstick.example.com", self.cert, self.dir,
                          unit=lambda: {"active": unit} if unit else {},
                          now=lambda: 1060)

    def test_no_name_is_off(self):
        self.assertEqual(tls.status("", self.cert, self.dir),
                         {"enabled": False, "state": "off"})

    def test_nothing_yet_is_missing(self):
        self.assertEqual(self.status()["state"], "missing")

    def test_an_attempt_that_has_not_asked_yet_is_checking(self):
        self.assertEqual(self.status("activating")["state"], "checking")

    def test_a_record_being_watched_for_is_waiting_and_shown(self):
        self.pending()
        got = self.status("activating")
        self.assertEqual(got["state"], "waiting")
        self.assertEqual(got["pending"]["record"],
                         "_acme-challenge.playstick.example.com")
        self.assertEqual(got["pending"]["value"], "abc123")
        self.assertEqual(got["pending"]["zone"], "example.com")

    def test_a_record_nobody_is_watching_for_is_record(self):
        self.pending()
        self.assertEqual(self.status("failed")["state"], "record")

    def test_a_garbled_pending_file_is_ignored(self):
        with open(os.path.join(self.dir, "pending.json"), "w") as handle:
            handle.write("{nope")
        self.assertEqual(self.status()["state"], "missing")

    @unittest.skipUnless(HAVE_OPENSSL, "needs the openssl binary")
    def test_a_certificate_is_valid_with_its_expiry(self):
        made, _key = make_cert(self.dir, "c")
        os.replace(made, self.cert)
        got = self.status()
        self.assertEqual(got["state"], "valid")
        self.assertTrue(got["certificate"])
        self.assertGreater(got["expires"], time.time())
        self.assertFalse(got["staging"])

    @unittest.skipUnless(HAVE_OPENSSL, "needs the openssl binary")
    def test_a_renewal_that_wants_a_record_still_reports_the_certificate(self):
        made, _key = make_cert(self.dir, "c")
        os.replace(made, self.cert)
        self.pending()
        got = self.status("activating")
        self.assertEqual(got["state"], "waiting")
        self.assertTrue(got["certificate"])


class UnitReport(unittest.TestCase):
    SHOW = ("ActiveState={active}\nResult={result}\nExecMainStatus={code}\n"
            "ExecMainStartTimestamp=@1795280400\nExecMainExitTimestamp={exit}\n"
            "InvocationID=abc123\n")

    def fake(self, active="inactive", result="success", code=0, exit="@1795280460",
             log="", show_rc=0):
        calls = []

        def run(argv, **_kw):
            calls.append(argv)
            if argv[0] == "systemctl":
                return subprocess.CompletedProcess(argv, show_rc, self.SHOW.format(
                    active=active, result=result, code=code, exit=exit), "")
            return subprocess.CompletedProcess(argv, 0, log, "")
        return run, calls

    def test_a_good_run_reports_its_times_and_log(self):
        run, calls = self.fake(log="Saving debug log\nSuccessfully received certificate.\n")
        got = tls.unit_report("playstick-tls.service", run=run)
        self.assertEqual((got["active"], got["result"], got["exit_status"]),
                         ("inactive", "success", 0))
        self.assertEqual((got["started"], got["finished"]), (1795280400, 1795280460))
        self.assertEqual(got["outcome"], "issued")
        self.assertEqual(got["log"][-1], "Successfully received certificate.")
        # The journal of this attempt only, not of every attempt ever.
        self.assertIn("_SYSTEMD_INVOCATION_ID=abc123", calls[1])
        self.assertIn("INVOCATION_ID=abc123", calls[1])

    def test_a_record_that_never_appeared_is_named(self):
        run, _ = self.fake(result="exit-code", code=1, log=(
            "waiting up to 900s for TXT _acme-challenge.x = y\n"
            "TXT _acme-challenge.x did not appear within 900s\n"))
        got = tls.unit_report("u", run=run)
        self.assertEqual((got["result"], got["exit_status"], got["outcome"]),
                         ("exit-code", 1, "no_record"))

    def test_nothing_due_is_named(self):
        run, _ = self.fake(log="Certificate not yet due for renewal; no action taken.\n")
        self.assertEqual(tls.unit_report("u", run=run)["outcome"], "not_due")

    def test_running_has_no_finish(self):
        run, _ = self.fake(active="activating", exit="")
        got = tls.unit_report("u", run=run)
        self.assertEqual(got["active"], "activating")
        self.assertIsNone(got["finished"])

    def test_no_systemctl_is_no_report(self):
        def run(*_a, **_k):
            raise FileNotFoundError("systemctl")
        self.assertEqual(tls.unit_report("u", run=run), {})

    def test_long_lines_are_cut(self):
        run, _ = self.fake(log="x" * 5000 + "\n")
        self.assertEqual(len(tls.unit_report("u", run=run)["log"][0]),
                         tls.LOG_LINE_CHARS)


class Routes(ApiTest):
    def test_the_status_route_reports_the_configured_name(self):
        directory = tempfile.mkdtemp(prefix="playstick-tls-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        with mock.patch.multiple(api, TLS_NAME="playstick.example.com",
                                 TLS_CERT=os.path.join(directory, "none.pem"),
                                 TLS_STATE_DIR=directory), \
                mock.patch.object(api, "_unit_report", lambda: {"active": "inactive"}):
            body = self.assertJson(self.fetch("/api/admin/tls"))
        self.assertEqual(body["state"], "missing")
        self.assertEqual(body["name"], "playstick.example.com")

    def test_off_says_so(self):
        with mock.patch.multiple(api, TLS_NAME=""):
            self.assertEqual(self.assertJson(self.fetch("/api/admin/tls")),
                             {"enabled": False, "state": "off"})

    def test_check_starts_the_unit_without_waiting_for_it(self):
        calls = []

        def fake_systemctl(*args, timeout=45):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.multiple(api, TLS_NAME="playstick.example.com",
                                 TLS_UNIT="playstick-tls.service",
                                 systemctl=fake_systemctl), \
                mock.patch.object(api, "_unit_report", lambda: {"active": "activating"}):
            resp = self.fetch("/api/admin/tls/check", method="POST", body={})
        self.assertEqual(resp.status, 200)
        self.assertIn(("start", "--no-block", "playstick-tls.service"), calls)

    def test_check_with_https_off_is_refused(self):
        with mock.patch.multiple(api, TLS_NAME=""):
            resp = self.fetch("/api/admin/tls/check", method="POST", body={})
        self.assertEqual(resp.status, 409)


if __name__ == "__main__":
    unittest.main()
