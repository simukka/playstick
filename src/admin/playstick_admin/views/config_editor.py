"""The Config tab: the app's settings, the Ansible variables, and actl.

Four sub-panels. First, and shown by default, the app's own settings -- the
share, the stick and how to log in to it -- with the one-off import of a
library prepared under the old arrangement beneath them. Then the folders the
Prepare page offers films from (sources_panel.py). Then, as Advanced, a
generated form of the top-level tunables in group_vars/all.yml,
edited in place so comments survive the round-trip. What is true of one site --
the stick's address and login, the NAS -- is not in there: it comes from
Settings, through core.site_vars, which is written before every actl run. And an actl console for the commands that need no become password --
a syntax check and a ping -- with a free-form box for anything else, which a
privileged deploy still needs a real terminal for.

Both work on the checkout named by settings.repo_root, read afresh each time,
so that an installed app -- which has no repo of its own -- can be pointed at
one in Settings without a restart. Until it is, the tab says so.
"""

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import paths
from ..core import config_file, credentials, site_vars
from ..bridges.process_runner import ActlRunner
from .settings_dialog import SettingsForm
from .sources_panel import SourcesPanel

# What the Advanced and Deploy panels say, in words rather than file names:
# the people reading them set the appliance up, they did not write it.
_NO_CHECKOUT = (
    "No playstick checkout to work on (repo root: %s).\n\n"
    "Advanced and Deploy work on a copy of playstick -- the one the package "
    "installs, or a clone. Set \"Repo root\" in Settings to one.")


class ConfigTab(QWidget):
    # Settings were saved: the main window re-reads what depends on them.
    settingsSaved = Signal()
    # "Import an existing prepared library...", which the main window owns
    # because it redraws every tab afterwards.
    importRequested = Signal()
    # The Sources tab added (True) or removed (False) a folder; Prepare scans
    # or redraws.
    sourcesChanged = Signal(bool)

    def __init__(self, settings, db, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._db = db
        self._doc = None
        self._path = ""
        self._widgets = {}          # key -> (widget, original_value)
        self._runner = ActlRunner(self)
        self._runner.output.connect(self._append_console)
        self._runner.finished.connect(lambda _c: self._set_running(False))
        # The password is only on disk while actl runs.
        self._runner.finished.connect(lambda _c: site_vars.clear_secrets())
        self._build()
        self._reload_file()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_settings_tab(), "Settings")
        self._sources = SourcesPanel(self._db)
        self._sources.changed.connect(self.sourcesChanged)
        self._tabs.addTab(self._sources, "Sources")
        self._tabs.addTab(self._build_vars_tab(), "Advanced")
        # Prepare can add a source too, and a scan changes the counts.
        self._tabs.currentChanged.connect(
            lambda i: self._sources.reload()
            if self._tabs.widget(i) is self._sources else None)
        self._console_page = self._build_console_tab()
        self._tabs.addTab(self._console_page, "Deploy / actl")
        self._tabs.setCurrentIndex(0)
        outer.addWidget(self._tabs)

    # --- settings ----------------------------------------------------------

    def _build_settings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        self._settings_form = SettingsForm(self._settings)
        self._settings_form.saved.connect(self._on_settings_saved)
        layout.addWidget(self._settings_form)

        old = QHBoxLayout()
        note = QLabel("A library prepared before this app kept its own "
                      "records? Read it in, keeping every film's id.")
        note.setObjectName("meta")
        note.setWordWrap(True)
        importer = QPushButton("Import an existing prepared library...")
        importer.clicked.connect(self.importRequested)
        old.addWidget(note, 1)
        old.addWidget(importer)
        layout.addLayout(old)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    def _on_settings_saved(self):
        # The repo root may have changed, and with it the files below.
        self._reload_file()
        self.settingsSaved.emit()

    # --- variables ---------------------------------------------------------

    def _build_vars_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        # One file: everything about a site is in Settings now, so the only
        # Ansible variables left to edit are the appliance's own.
        top = QHBoxLayout()
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._reload_file)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save_file)
        where = QLabel("How the appliance behaves -- the same for every "
                       "site. What is particular to this one is in Settings.")
        where.setObjectName("meta")
        top.addWidget(where, 1)
        top.addWidget(reload_btn)
        top.addWidget(save_btn)
        layout.addLayout(top)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._form_area = QScrollArea()
        self._form_area.setWidgetResizable(True)
        self._form_host = QWidget()
        self._form_area.setWidget(self._form_host)
        layout.addWidget(self._form_area, 1)
        return page

    def refresh(self):
        """Re-read the settings and the file, for when the first-run window
        has just saved -- and may have named another repo."""
        self._settings_form.load()
        self._reload_file()

    def _reload_file(self):
        root = self._settings.repo_root
        self._widgets = {}
        host = QWidget()
        vbox = QVBoxLayout(host)
        if not paths.is_checkout(root):
            self._doc = None
            self._path = ""
            self._status.setText("")
            vbox.addWidget(QLabel(_NO_CHECKOUT % (root or "not set")))
            vbox.addStretch(1)
            self._form_host = host
            self._form_area.setWidget(host)
            return
        self._path = paths.group_vars(root)
        if not self._path or not os.path.isfile(self._path):
            self._doc = None
            vbox.addWidget(QLabel("This copy of playstick has no appliance "
                                  "settings to edit."))
            self._form_host = host
            self._form_area.setWidget(host)
            return
        if not config_file.HAVE_RUAMEL:
            self._doc = None
            self._status.setText("Read-only: editing these needs the "
                                 "python3-ruamel.yaml package.")
            text = QPlainTextEdit(config_file.read_text(self._path))
            text.setReadOnly(True)
            vbox.addWidget(text)
            self._form_host = host
            self._form_area.setWidget(host)
            return
        try:
            self._doc = config_file.load(self._path)
        except Exception as exc:
            self._doc = None
            vbox.addWidget(QLabel("The appliance settings could not be "
                                  "read: %s" % exc))
            self._form_host = host
            self._form_area.setWidget(host)
            return
        self._status.setText("Changes reach the stick with its next update.")
        self._populate_form(vbox)
        self._form_host = host
        self._form_area.setWidget(host)

    def _populate_form(self, vbox):
        groups = {}
        for key in self._doc:
            value = self._doc[key]
            groups.setdefault(config_file.group_for(key), []).append((key, value))
        for label in sorted(groups):
            box = QGroupBox(label)
            form = QFormLayout(box)
            for key, value in groups[label]:
                widget = self._widget_for(key, value)
                form.addRow(key, widget)
            vbox.addWidget(box)
        vbox.addStretch(1)

    def _widget_for(self, key, value):
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
            self._widgets[key] = (w, value)
            return w
        if isinstance(value, (list, tuple)) and all(
                isinstance(v, (str, int, float)) for v in value):
            w = QLineEdit(", ".join(str(v) for v in value))
            self._widgets[key] = (w, list(value))
            return w
        if isinstance(value, (str, int, float)) or value is None:
            w = QLineEdit("" if value is None else str(value))
            self._widgets[key] = (w, value)
            return w
        # Nested map or something exotic: leave it to the raw file.
        label = QLabel("(not editable here)")
        label.setEnabled(False)
        return label

    def _save_file(self):
        if self._doc is None:
            return
        for key, (widget, original) in self._widgets.items():
            self._doc[key] = self._read_widget(widget, original)
        try:
            config_file.save(self._path, self._doc)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._status.setText("Saved. The stick has them after its next "
                             "update.")

    @staticmethod
    def _read_widget(widget, original):
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        text = widget.text().strip()
        if isinstance(original, list):
            return [p.strip() for p in text.split(",") if p.strip()]
        if isinstance(original, bool):
            return text.lower() in ("true", "yes", "1")
        if isinstance(original, int) and text.lstrip("-").isdigit():
            return int(text)
        if isinstance(original, float):
            try:
                return float(text)
            except ValueError:
                return text
        return text

    # --- actl console ------------------------------------------------------

    def _build_console_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        note = QLabel(
            "Runs ./actl in the repo. The presets need no password. To "
            "deploy, use Update device on the Device page: it asks for the "
            "stick's sudo password and runs the playbook here.")
        note.setWordWrap(True)
        layout.addWidget(note)

        presets = QHBoxLayout()
        for label, command in (
                ("Syntax check", ""),
                ("Ping device", "ansible -m ping vivostick"),
                ("Gather facts", "ansible -m setup vivostick")):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, c=command: self._run(c))
            presets.addWidget(btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._runner.stop)
        presets.addStretch(1)
        presets.addWidget(self._stop_btn)
        layout.addLayout(presets)

        cmd_row = QHBoxLayout()
        self._cmd = QLineEdit()
        self._cmd.setPlaceholderText("ansible-playbook site.yml --check --diff")
        self._cmd.returnPressed.connect(lambda: self._run(self._cmd.text().strip()))
        run_btn = QPushButton("Run")
        run_btn.setObjectName("accent")
        run_btn.clicked.connect(lambda: self._run(self._cmd.text().strip()))
        cmd_row.addWidget(self._cmd, 1)
        cmd_row.addWidget(run_btn)
        layout.addLayout(cmd_row)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(10000)
        layout.addWidget(self._console, 1)
        return page

    def deploy(self, become_password=""):
        """Update the stick: the whole playbook, in this console. Returns
        False when something else is already running."""
        self._tabs.setCurrentWidget(self._console_page)
        return self._run("ansible-playbook site.yml",
                         become_password=become_password)

    def _run(self, command, become_password=""):
        if self._runner.is_running():
            return False
        root = self._settings.repo_root
        if not paths.is_checkout(root):
            self._append_console(_NO_CHECKOUT % (root or "not set") + "\n")
            return False
        site_vars.write_site(self._settings)
        nas_password = (credentials.get_nas_password()
                        if self._settings.nas_username else "")
        # Both only for as long as this run: the runner's finished signal
        # clears them -- see __init__.
        if nas_password or become_password:
            site_vars.write_secrets(nas_password=nas_password,
                                    ansible_become_password=become_password)
        self._set_running(True)
        self._runner.run(root, command)
        return True

    def _set_running(self, running):
        self._stop_btn.setEnabled(running)

    def _append_console(self, text):
        cursor = self._console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self._console.setTextCursor(cursor)
