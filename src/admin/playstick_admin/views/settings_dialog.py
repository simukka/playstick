"""The app's settings: a form in the Config tab, and the window that sets it up.

The first time, the form is a wizard in a window of its own: where the share
and the stick are; how to log in to the stick, with a test; and the share as
the stick mounts it. Afterwards it is the Settings tab under Config, all three
on one page, plus the TMDb language and the repo root, which the first run
leaves out because their defaults are right for nearly everybody.

Every site value a deploy needs is set here and nowhere else -- core.site_vars
writes them where actl hands them to Ansible. The NAS password goes to the
keyring (core.credentials), and the field is never filled back in: showing it
would mean reading the keyring every time the window opens.

The NAS root is checked for writability the moment it is set rather than at the
moment a job fails. The stick mounts the share read-only and this machine is the
one that puts films on it, so a read-only mount here produces a working scan, a
working encode, and a failure at the last step -- an hour later, every time.

The login test says what to do when it fails, and does not stop anybody
finishing: the stick may well be switched off while the app is set up.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import theme
from ..bridges import net_worker
from ..core import credentials, nas_mount, site_vars, ssh_check
from .device_status_bar import COLOURS

# Where a key comes from: an account, then Settings -- API on the site.
TMDB_SIGNUP = "https://www.themoviedb.org/signup"


class _PathRow(QWidget):
    def __init__(self, initial="", pick_dir=True, start=""):
        super().__init__()
        self._pick_dir = pick_dir
        self._start = start
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(initial)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self):
        where = self.edit.text() or self._start
        if self._pick_dir:
            path = QFileDialog.getExistingDirectory(self, "Choose folder", where)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose file", where)
        if path:
            self.edit.setText(path)


class _Probe:
    """Just enough of Settings to reuse its check against unsaved text."""

    def __init__(self, nas_root):
        self.nas_root = nas_root

    def problem(self):
        from ..settings import Settings
        return Settings.nas_root_problem(self)


def _align(*forms):
    """One label column across several forms, so two sections of one window
    line their fields up rather than each sizing to its own longest label."""
    labels = [form.itemAt(row, QFormLayout.LabelRole).widget()
              for form in forms for row in range(form.rowCount())
              if form.itemAt(row, QFormLayout.LabelRole) is not None]
    labels = [label for label in labels if label is not None]
    width = max((label.sizeHint().width() for label in labels), default=0)
    for label in labels:
        label.setMinimumWidth(width)


def _heading(text):
    label = QLabel(text)
    label.setObjectName("section")
    return label


class SettingsForm(QWidget):
    """Every setting, saved together. `first_run` lays it out as the wizard's
    three steps; otherwise as one page with a Save button, for the tab."""

    saved = Signal()
    cancelled = Signal()

    def __init__(self, settings, parent=None, first_run=False):
        super().__init__(parent)
        self._settings = settings
        self._first_run = first_run
        self._testing = False

        # --- the share and the stick ---------------------------------------
        self._nas = _PathRow(settings.nas_root, pick_dir=True)
        self._nas.edit.textChanged.connect(self._check_nas)
        self._nas_note = QLabel("")
        self._nas_note.setWordWrap(True)
        self._device = QLineEdit(settings.device_ip)
        self._device.setPlaceholderText("192.168.1.50, or vivostick.local")
        self._tmdb = QLineEdit(settings.tmdb_key)
        self._tmdb_lang = QLineEdit(settings.tmdb_language)
        self._repo = _PathRow(settings.repo_root, pick_dir=True)

        basics = QWidget()
        form = QFormLayout(basics)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("NAS root (the share, as mounted here)", self._nas)
        form.addRow("", self._nas_note)
        form.addRow("Device IP", self._device)
        tmdb_label = QLabel('<a href="%s">TMDb API key</a>' % TMDB_SIGNUP)
        tmdb_label.setOpenExternalLinks(True)
        tmdb_label.setToolTip("Free, with a TMDb account: " + TMDB_SIGNUP)
        form.addRow(tmdb_label, self._tmdb)
        # Not on the first run, like the repo root: en-US is right until
        # somebody wants titles and plots in another language.
        if not first_run:
            form.addRow("TMDb language", self._tmdb_lang)
            form.addRow("Repo root (for actl)", self._repo)

        # --- logging in to it -----------------------------------------------
        self._ssh_user = QLineEdit(settings.ssh_user)
        self._ssh_user.setPlaceholderText("the account on the stick")
        self._ssh_key = _PathRow(
            settings.ssh_key, pick_dir=False,
            start=os.path.join(os.path.expanduser("~"), ".ssh"))
        self._ssh_key.edit.setPlaceholderText(
            "empty: the ssh agent and ~/.ssh/id_*")
        self._test_btn = QPushButton("Test connection")
        self._test_btn.setObjectName("accent")
        self._test_btn.clicked.connect(self._test)
        self._test_note = QLabel("")
        self._test_note.setWordWrap(True)
        self._test_note.setTextInteractionFlags(Qt.TextSelectableByMouse)

        login = QWidget()
        ssh_form = QFormLayout(login)
        ssh_form.setContentsMargins(0, 0, 0, 0)
        if first_run:
            blurb = QLabel("Deploying to the stick logs in over ssh with a "
                           "key, the way Ansible will -- no password.")
            blurb.setObjectName("subheading")
            blurb.setWordWrap(True)
            ssh_form.addRow(blurb)
        ssh_form.addRow("SSH user", self._ssh_user)
        ssh_form.addRow("SSH key", self._ssh_key)
        test_row = QHBoxLayout()
        test_row.addWidget(self._test_btn)
        test_row.addStretch(1)
        ssh_form.addRow("", test_row)
        ssh_form.addRow("", self._test_note)

        # --- the share, as the stick mounts it -------------------------------
        self._nas_server = QLineEdit(settings.nas_server)
        self._nas_server.setPlaceholderText("192.168.1.5 -- an address, so the "
                                            "mount does not wait on mDNS")
        self._nas_share = QLineEdit(settings.nas_share)
        self._nas_share.setPlaceholderText("the exported name: video, "
                                           "not /volume1/video")
        self._nas_user = QLineEdit(settings.nas_username)
        self._nas_user.setPlaceholderText("empty: mount as guest")
        self._nas_password = QLineEdit()
        self._nas_password.setEchoMode(QLineEdit.Password)
        self._nas_password.setPlaceholderText(
            "kept in the keyring; empty keeps what is there")
        self._nas_mac = QLineEdit(settings.nas_mac)
        self._nas_mac.setPlaceholderText(
            "optional, for wake-on-LAN: 00:11:32:aa:bb:cc")
        self._suggest_share(settings.nas_root)
        self._nas.edit.textChanged.connect(self._suggest_share)

        share = QWidget()
        share_form = QFormLayout(share)
        share_form.setContentsMargins(0, 0, 0, 0)
        if first_run:
            blurb = QLabel("The stick mounts the share itself, read-only. "
                           "Filled in from this machine's mount when it can "
                           "be.")
            blurb.setObjectName("subheading")
            blurb.setWordWrap(True)
            share_form.addRow(blurb)
        share_form.addRow("NAS server", self._nas_server)
        share_form.addRow("Share name", self._nas_share)
        share_form.addRow("NAS user", self._nas_user)
        share_form.addRow("NAS password", self._nas_password)
        share_form.addRow("NAS MAC address", self._nas_mac)

        outer = QVBoxLayout(self)
        buttons = QHBoxLayout()
        buttons.addStretch(1)

        if first_run:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.cancelled)
            self._pages = QStackedWidget()
            self._pages.addWidget(basics)
            self._pages.addWidget(login)
            self._pages.addWidget(share)
            self._step = _heading("")
            outer.addWidget(self._step)
            outer.addWidget(self._pages, 1)
            self._back = QPushButton("Back")
            self._back.clicked.connect(
                lambda: self._go(self._pages.currentIndex() - 1))
            self._next = QPushButton("Next")
            self._next.setObjectName("primary")
            self._next.clicked.connect(
                lambda: self._go(self._pages.currentIndex() + 1))
            self._finish = QPushButton("Finish")
            self._finish.setObjectName("primary")
            self._finish.clicked.connect(self._save)
            for button in (cancel, self._back, self._next, self._finish):
                buttons.addWidget(button)
            self._go(0)
        else:
            _align(form, ssh_form, share_form)
            outer.addWidget(_heading("The share and the stick"))
            outer.addWidget(basics)
            outer.addSpacing(10)
            outer.addWidget(_heading("Logging in to the stick"))
            outer.addWidget(login)
            outer.addSpacing(10)
            outer.addWidget(_heading("The share, as the stick mounts it"))
            outer.addWidget(share)
            outer.addSpacing(6)
            # Puts back what is saved, for a form somebody has half-edited.
            revert = QPushButton("Revert")
            revert.clicked.connect(self.load)
            save = QPushButton("Save")
            save.setObjectName("primary")
            save.clicked.connect(self._save)
            buttons.addWidget(revert)
            buttons.addWidget(save)
        outer.addLayout(buttons)
        if not first_run:
            outer.addStretch(1)

    def load(self):
        """Show what is saved -- after the first-run window, or a revert. The
        password field is left empty, as it always is: see the docstring."""
        s = self._settings
        self._nas.edit.setText(s.nas_root)
        self._device.setText(s.device_ip)
        self._tmdb.setText(s.tmdb_key)
        self._tmdb_lang.setText(s.tmdb_language)
        self._repo.edit.setText(s.repo_root)
        self._ssh_user.setText(s.ssh_user)
        self._ssh_key.edit.setText(s.ssh_key)
        self._nas_server.setText(s.nas_server)
        self._nas_share.setText(s.nas_share)
        self._nas_user.setText(s.nas_username)
        self._nas_mac.setText(s.nas_mac)
        self._nas_password.clear()
        self._test_note.clear()

    STEPS = ("the share and the stick", "logging in to the stick",
             "the share, as the stick mounts it")

    def _go(self, index):
        self._pages.setCurrentIndex(index)
        self._step.setText("Step %d of %d: %s"
                           % (index + 1, len(self.STEPS), self.STEPS[index]))
        last = index == len(self.STEPS) - 1
        self._back.setVisible(index > 0)
        self._next.setVisible(not last)
        self._finish.setVisible(last)

    def _suggest_share(self, nas_root):
        """Fill the stick's view of the share from this machine's mount --
        only into fields nobody has filled in."""
        found = nas_mount.detect(nas_root.strip())
        if not found:
            return
        for field, value in zip((self._nas_server, self._nas_share,
                                 self._nas_user), found):
            if value and not field.text().strip():
                field.setText(value)

    def _check_nas(self, text):
        """Say what is wrong with this share now, not in an hour."""
        probe = _Probe(text.strip())
        problem = probe.problem()
        self._nas_note.setText(problem or "")
        self._nas_note.setStyleSheet(
            "color: %s;" % theme.DANGER if problem else "")

    # --- the login test ----------------------------------------------------

    def _test(self):
        if self._testing:
            return
        host = ssh_check.host_of(self._device.text())
        user = self._ssh_user.text().strip()
        key = self._ssh_key.edit.text().strip()
        self._testing = True
        self._test_btn.setEnabled(False)
        self._show_result(None, "Connecting to %s..." % (host or "the stick"))
        job, thread = net_worker.call(
            lambda: ssh_check.test(host, user, key), label="ssh")
        # Methods rather than lambdas: a dialog closed mid-test must not be
        # reached by the answer.
        job.done.connect(self._on_tested)
        job.failed.connect(self._on_test_failed)
        thread.start()

    def _on_tested(self, result):
        ok, sentence = result
        self._show_result(ok, sentence)

    def _on_test_failed(self, message):
        self._show_result(False, message)

    def _show_result(self, ok, sentence):
        if ok is not None:
            self._testing = False
            self._test_btn.setEnabled(True)
        colour = ("" if ok is None else
                  "color: %s;" % (COLOURS["ok"] if ok else theme.DANGER))
        self._test_note.setStyleSheet(colour)
        self._test_note.setText(sentence)
        # Taller than the line it replaced, often -- the fix is on a line of
        # its own -- and a window does not grow to fit a label by itself.
        if self._first_run:
            self.window().adjustSize()

    # --- saving -------------------------------------------------------------

    def _save(self):
        s = self._settings
        s.nas_root = self._nas.edit.text().strip()
        s.device_ip = self._device.text()
        s.tmdb_key = self._tmdb.text().strip()
        s.tmdb_language = self._tmdb_lang.text().strip() or "en-US"
        s.ssh_user = self._ssh_user.text().strip()
        s.ssh_key = self._ssh_key.edit.text().strip()
        s.nas_server = self._nas_server.text().strip()
        s.nas_share = self._nas_share.text().strip()
        s.nas_username = self._nas_user.text().strip()
        s.nas_mac = self._nas_mac.text().strip()
        if not self._first_run:
            s.repo_root = self._repo.edit.text().strip()
        site_vars.write_site(s)
        password = self._nas_password.text()
        try:
            if not s.nas_username:
                credentials.set_nas_password("")      # guest: nothing to keep
            elif password:
                credentials.set_nas_password(password)
        except credentials.CredentialError as exc:
            QMessageBox.warning(self, "NAS password not saved", str(exc))
        self._nas_password.clear()
        self.saved.emit()


class SettingsDialog(QDialog):
    """The first-run window: the form as a wizard, nothing else."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Get started")
        self.setMinimumWidth(560)
        self.form = SettingsForm(settings, self, first_run=True)
        self.form.saved.connect(self.accept)
        self.form.cancelled.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.form)
