"""The Device tab's HTTPS box: the certificate, or the DNS record it needs.

Shown only when Settings names an HTTPS name. The stick proves that name to
Let's Encrypt with a TXT record, and nothing on the stick can add one -- there
is deliberately no DNS provider in it (see roles/tls). So when certbot wants a
record, the stick's challenge hook writes it down, the daemon reports it at
/api/admin/tls, and this box shows it: the name and value to copy into the DNS
provider's form, and a button that starts another attempt once it is added.

The stick watches the domain's own name servers while an attempt runs, so
"Check now" is only needed after an attempt has given up. Pressing it while
one is still waiting is harmless.

Polled every ten seconds while the Device tab is on screen and a name is set,
and not at all otherwise: an expiry date does not need watching from a
background tab. Every two seconds after "Check now", until the attempt it
started has finished -- and then the box says how it went, in a sentence, with
the attempt's own log one click away. A button that starts something on another
machine and then says nothing is a button people press again.
"""

import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..bridges import net_worker
from ..core.device_client import DeviceClient

POLL_MS = 10_000
# "Check now" was pressed and the stick has not answered yet.
_AWAITING = object()
# While an attempt that "Check now" started is running.
FAST_POLL_MS = 2_000

# What the last attempt came to, in the words of somebody who pressed a button.
# The keys are tls.unit_report's outcomes.
_OUTCOME_WORDS = {
    "issued": "a certificate was issued",
    "not_due": "the certificate is not due for renewal, so nothing changed",
    "no_record": "the TXT record did not appear on the name servers in time",
    "record_seen": "the TXT record was found",
    "rate_limited": "Let's Encrypt refused: too many certificates recently",
    "challenge_failed": "Let's Encrypt could not verify the TXT record",
}


def _clock(when):
    return time.strftime("%H:%M", time.localtime(when)) if when else "?"


def describe_last(last):
    """One line about the last attempt, or "" when there is nothing to say.
    Qt-free, for tests."""
    if not last or not last.get("ran"):
        return "No check has run since the stick started." if last else ""
    words = _OUTCOME_WORDS.get(last.get("outcome", ""), "")
    if last.get("active") == "activating":
        return ("Checking now (started %s)%s." % (
            _clock(last.get("started")), ": " + words if words else ""))
    if last.get("result") == "success":
        return "Last check at %s worked%s." % (
            _clock(last.get("finished")), ": " + words if words else "")
    return "Last check at %s failed%s%s. See the log." % (
        _clock(last.get("finished")),
        " (exit %s)" % last["exit_status"] if last.get("exit_status") else "",
        ": " + words if words else "")


def describe(status, now=None):
    """One or two sentences for a /api/admin/tls answer. Qt-free, for tests."""
    now = time.time() if now is None else now
    state = status.get("state", "")
    expires = status.get("expires")
    served = ""
    if status.get("certificate") and expires:
        served = "Certificate valid until %s%s." % (
            time.strftime("%d %B %Y", time.localtime(expires)),
            " (Let's Encrypt staging: browsers will warn)"
            if status.get("staging") else "")
    pending = status.get("pending") or {}
    if state == "valid":
        return served + " Renewal is automatic until it needs a new DNS record."
    if state == "waiting":
        left = ""
        if pending.get("since") and pending.get("wait"):
            minutes = max(0, int((pending["since"] + pending["wait"] - now) // 60))
            left = " for about %d more minute%s" % (minutes,
                                                     "" if minutes == 1 else "s")
        lead = ("Renewal needs a new DNS record. " if status.get("certificate")
                else "")
        return (lead + "Add this TXT record at your DNS provider. The stick is "
                "watching the domain's name servers for it%s and carries on "
                "by itself when it appears. %s" % (left, served)).strip()
    if state == "record":
        return ("The stick stopped waiting for this TXT record. Add it at your "
                "DNS provider (or check it is exactly as below), then press "
                "Check now. %s" % served).strip()
    if state == "checking":
        return "Asking Let's Encrypt..."
    if state == "missing":
        return ("No certificate yet, and nothing is running. Press Check now "
                "to start, or update the stick if it has not been since the "
                "HTTPS name was set.")
    if state == "off":
        return ("The stick has no HTTPS name yet. Update it to send the one in "
                "Settings.")
    return "The stick did not say."


def relative_name(pending):
    """The record name as most providers' forms want it: inside the zone."""
    record = pending.get("record", "")
    zone = pending.get("zone", "")
    if zone and record.endswith("." + zone):
        return record[:-len(zone) - 1]
    return record


class HttpsBox(QGroupBox):
    def __init__(self, settings, parent=None):
        super().__init__("HTTPS", parent)
        self._settings = settings
        self._busy = False

        layout = QVBoxLayout(self)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        self._record = QWidget()
        form = QFormLayout(self._record)
        form.setContentsMargins(0, 0, 0, 0)
        self._type = QLabel("TXT")
        self._name = self._copyable()
        self._zone = QLabel("")
        self._zone.setObjectName("meta")
        self._value = self._copyable()
        form.addRow("Type", self._type)
        form.addRow("Name", self._name[0])
        form.addRow("", self._zone)
        form.addRow("Value", self._value[0])
        ttl = QLabel("TTL: the lowest your provider allows. The record can be "
                     "deleted once the certificate is issued.")
        ttl.setObjectName("meta")
        ttl.setWordWrap(True)
        form.addRow("", ttl)
        layout.addWidget(self._record)
        self._record.setVisible(False)

        self._last = QLabel("")
        self._last.setWordWrap(True)
        layout.addWidget(self._last)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(200)
        self._log.setMinimumHeight(140)
        self._log.setVisible(False)
        layout.addWidget(self._log)

        row = QHBoxLayout()
        self._log_btn = QPushButton("Show log")
        self._log_btn.setEnabled(False)
        self._log_btn.clicked.connect(
            lambda: self._show_log(not self._log.isVisible()))
        row.addWidget(self._log_btn)
        row.addStretch(1)
        self._check = QPushButton("Check now")
        self._check.setToolTip("Start a certificate attempt on the stick now "
                               "rather than at its daily check.")
        self._check.clicked.connect(self._check_now)
        row.addWidget(self._check)
        layout.addLayout(row)

        # Set by "Check now": the press time, until the attempt after it ends.
        self._checking_since = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.refresh)
        self.setVisible(bool(settings.tls_name))

    def _copyable(self):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        edit.setReadOnly(True)
        button = QPushButton("Copy")
        button.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(edit.text()))
        row.addWidget(edit, 1)
        row.addWidget(button)
        return holder, edit

    # --- lifecycle, driven by the Device tab ----------------------------------

    def start(self):
        """The Device tab came on screen, or Settings were saved."""
        wanted = bool(self._settings.tls_name)
        self.setVisible(wanted)
        if not wanted or not self._settings.device_url:
            self._timer.stop()
            return
        self.refresh()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    # --- talking to the stick -------------------------------------------------

    def refresh(self):
        if self._busy or not self._settings.device_url:
            return
        self._run(DeviceClient(self._settings.device_url).tls_status)

    def _check_now(self):
        if not self._settings.device_url:
            return
        self._check.setEnabled(False)
        # Anchored to the stick's clock by its first answer (show_status),
        # not to this machine's: the two need not agree, and comparing across
        # them could wait forever for an attempt that has long finished.
        self._checking_since = _AWAITING
        self._last.setText("Asking the stick to check...")
        self._timer.setInterval(FAST_POLL_MS)
        self._timer.start()
        self._run(DeviceClient(self._settings.device_url).tls_check)

    def _show_log(self, on):
        self._log.setVisible(on)
        self._log_btn.setText("Hide log" if on else "Show log")

    def _run(self, fn, after=None):
        self._busy = True
        job, thread = net_worker.call(fn, label="https")

        def done(result):
            self._busy = False
            self.show_status(result or {})
            if after:
                after()

        def failed(message):
            self._busy = False
            self._note.setText("The stick did not answer about HTTPS: %s"
                               % message)
            if self._checking_since is not None:
                self._finish_check()
            if after:
                after()
        job.done.connect(done)
        job.failed.connect(failed)
        thread.start()

    def _finish_check(self):
        self._checking_since = None
        self._check.setEnabled(True)
        self._timer.setInterval(POLL_MS)

    def show_status(self, status):
        self._note.setText(describe(status))
        last = status.get("last") or {}
        line = describe_last(last)
        if self._checking_since is _AWAITING and status.get("now"):
            # A little early, so an attempt that systemd started in the same
            # second as the reply still counts as the one asked for.
            self._checking_since = status["now"] - 2
        if self._checking_since is not None:
            if not last:
                # This stick cannot report (no systemd): stop waiting.
                self._finish_check()
            elif last.get("active") == "activating":
                pass                    # the line already says it is checking
            elif (self._checking_since is not _AWAITING
                  and (last.get("finished") or 0) >= self._checking_since):
                self._finish_check()
                if last.get("result") != "success":
                    self._show_log(True)
            else:
                line = "Asking the stick to check..."   # not started yet
        self._last.setText(line)
        text = "\n".join(last.get("log") or [])
        if text != self._log.toPlainText():      # keep the scroll while reading
            self._log.setPlainText(text)
        self._log_btn.setEnabled(bool(last.get("log")))
        pending = status.get("pending") or {}
        show = status.get("state") in ("waiting", "record") and bool(pending)
        self._record.setVisible(show)
        if show:
            self._name[1].setText(relative_name(pending))
            zone = pending.get("zone", "")
            self._zone.setText(
                ("in the zone %s -- the full name is %s"
                 % (zone, pending.get("record", ""))) if zone
                else "the full name; drop your domain from the end if your "
                     "provider adds it")
            self._value[1].setText(pending.get("value", ""))
