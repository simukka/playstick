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

The projector's model, serial port and input are select lists rather than text
boxes (the tables are in core.config_file), and the Projector group has a
"Probe the stick" button: it asks the stick's /api/admin/projector/probe which
serial adapters it has, what the screen's EDID calls itself and which port a
projector answers on, and picks the model and port from that. It fills the form
and saves nothing -- Save still does that.

The installed copy (/usr/share/playstick) belongs to root, so Save cannot write
its all.yml. There it keeps what differs from the file in the app instead --
site_vars.save_overrides -- and those reach the stick with the next update,
because they are merged into the inventory file actl hands Ansible. The form
always shows what a deploy would use: the file with those overrides applied.
"""

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import paths
from ..core import config_file, credentials, release, site_vars
from ..bridges import net_worker
from ..bridges.process_runner import ActlRunner
from ..core.device_client import DeviceClient
from .settings_dialog import SettingsForm
from .sources_panel import SourcesPanel

# What the Advanced and Deploy panels say, in words rather than file names:
# the people reading them set the appliance up, they did not write it.
_NO_CHECKOUT = (
    "No playstick checkout to work on (repo root: %s).\n\n"
    "Advanced and Deploy work on a copy of playstick -- the one the package "
    "installs, or a clone. Set \"Repo root\" in Settings to one.")


def _find(combo, value):
    return combo.findData(value)


def _select(combo, value):
    i = _find(combo, value)
    if i >= 0:
        combo.setCurrentIndex(i)


def _value(combo):
    """The value behind what the box shows. An editable box may hold text
    somebody typed, which matches no item and is the value itself."""
    if combo.isEditable():
        text = combo.currentText()
        i = combo.findText(text)
        return combo.itemData(i) if i >= 0 else text.strip()
    return combo.currentData() or ""


def _plain(value):
    """A ruamel value as the plain type the form reads back, for comparing.
    None is "" because an empty text box is how the form says it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    for kind in (int, float, str):
        if isinstance(value, kind):
            return kind(value)
    return value


def _port_label(port):
    notes = [port.get("target") or ""]
    if port.get("ftdi"):
        notes.append("FTDI")
    if port.get("answers"):
        notes.append("projector answers, lamp %s" % (port.get("power") or "?"))
    elif port.get("error"):
        notes.append("no answer")
    return "%s  (%s)" % (port.get("path", ""), ", ".join(n for n in notes if n))


class ConfigTab(QWidget):
    # Settings were saved: the main window re-reads what depends on them.
    settingsSaved = Signal()
    # "Import an existing prepared library...", which the main window owns
    # because it redraws every tab afterwards.
    importRequested = Signal()
    # The Sources tab added (True) or removed (False) a folder; Prepare scans
    # or redraws.
    sourcesChanged = Signal(bool)
    # "Run all" on the Deploy panel: the whole playbook, which needs the
    # stick's sudo password. The main window asks for it, as it does for
    # Update device, and hands it back through deploy().
    runAllRequested = Signal()

    def __init__(self, settings, db, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._db = db
        self._doc = None
        self._path = ""
        self._writable = False
        self._widgets = {}          # key -> (widget, original_value)
        self._choices = {}          # projector key -> its QComboBox
        # Bumped on every reload, so a probe that answers after the form was
        # rebuilt does not write into combo boxes that no longer exist.
        self._form_generation = 0
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
        self._tabs.addTab(self._console_page, "Deploy")
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
        self._choices = {}
        self._form_generation += 1
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
        self._writable = os.access(self._path, os.W_OK)
        overrides = site_vars.load_overrides()
        if self._writable:
            note = "Changes reach the stick with its next update."
            if overrides:
                note += (" %d value(s) saved earlier by the installed copy "
                         "override this file (%s); saving here writes them "
                         "into it." % (len(overrides),
                                       ", ".join(sorted(overrides))))
        else:
            note = ("This is the installed copy, which belongs to root, so "
                    "changes are kept by this app and override it with the "
                    "stick's next update.")
            if overrides:
                note += " Overridden now: %s." % ", ".join(sorted(overrides))
        self._status.setText(note)
        self._populate_form(vbox, overrides)
        self._form_host = host
        self._form_area.setWidget(host)

    def _populate_form(self, vbox, overrides=None):
        overrides = overrides or {}
        groups = {}
        for key in self._doc:
            value = overrides.get(key, self._doc[key])
            groups.setdefault(config_file.group_for(key), []).append((key, value))
        for label in sorted(groups):
            box = QGroupBox(label)
            form = QFormLayout(box)
            for key, value in groups[label]:
                widget = self._widget_for(key, value)
                form.addRow(key, widget)
            if any(k in config_file.CHOICES for k, _v in groups[label]):
                form.addRow(self._probe_row())
            vbox.addWidget(box)
        vbox.addStretch(1)

    def _widget_for(self, key, value):
        if key in config_file.CHOICES:
            return self._choice_widget(key, value)
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

    # --- the projector's select lists ----------------------------------------

    def _choice_widget(self, key, value):
        w = QComboBox()
        w.setEditable(key in config_file.EDITABLE_CHOICES)
        for option, label in config_file.choices_for(key, value):
            w.addItem(label, option)
        current = "" if value is None else str(value)
        if key == config_file.PROJECTOR_INPUT_KEY:
            current = current.upper()
        _select(w, current)
        self._widgets[key] = (w, value)
        self._choices[key] = w
        return w

    def _probe_row(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton("Probe the stick")
        button.setToolTip("Ask the stick which serial adapters it has, what "
                          "the screen on its HDMI port is, and which port a "
                          "projector answers on. Only asks; changes nothing.")
        note = QLabel("Fill the model and serial port from what is plugged "
                      "in. Nothing is saved until Save.")
        note.setObjectName("meta")
        note.setWordWrap(True)
        button.clicked.connect(lambda: self._probe(button, note))
        layout.addWidget(button)
        layout.addWidget(note, 1)
        return row

    def _probe(self, button, note):
        url = self._settings.device_url
        if not url:
            note.setText("Set the stick's address in Settings first.")
            return
        generation = self._form_generation
        button.setEnabled(False)
        note.setText("Asking the stick...")
        job, thread = net_worker.call(DeviceClient(url).projector_probe,
                                      label="probe")

        def current():
            return generation == self._form_generation

        def done(result):
            if current():
                button.setEnabled(True)
                note.setText(self._apply_probe(result or {}))

        def failed(message):
            if current():
                button.setEnabled(True)
                note.setText("The stick did not answer: %s" % message)
        job.done.connect(done)
        job.failed.connect(failed)
        thread.start()

    def _apply_probe(self, result):
        """Pick what the probe suggests and return one line saying what it
        found. Only the model and the port are selected; the input the
        projector is showing is marked, because it is the stick's input only
        if the stick is what is on screen."""
        suggested = result.get("suggested") or {}
        ports = result.get("ports") or []

        device = self._choices.get(config_file.PROJECTOR_DEVICE_KEY)
        if device is not None:
            keep = _value(device)
            device.clear()
            for option, label in config_file.PROJECTOR_DEVICES:
                device.addItem(label, option)
            for port in ports:
                device.addItem(_port_label(port), port.get("path", ""))
            if _find(device, keep) < 0:
                device.addItem(keep, keep)
            _select(device, suggested.get("device") or keep)

        model = self._choices.get(config_file.PROJECTOR_MODEL_KEY)
        if model is not None and suggested.get("model"):
            if _find(model, suggested["model"]) < 0:
                model.addItem(suggested["model"], suggested["model"])
            _select(model, suggested["model"])

        showing = suggested.get("input") or ""
        inputs = self._choices.get(config_file.PROJECTOR_INPUT_KEY)
        if inputs is not None:
            for option, label in config_file.PROJECTOR_INPUTS:
                i = _find(inputs, option)
                if i >= 0:
                    inputs.setItemText(i, label + (" -- showing now"
                                                   if option and option == showing
                                                   else ""))

        said = []
        names = [d.get("name") for d in result.get("displays") or []
                 if d.get("connected") and d.get("name")]
        if names:
            said.append("the screen calls itself %s" % ", ".join(names))
        answering = [p for p in ports if p.get("answers")]
        if answering:
            p = answering[0]
            said.append("a projector answered on %s, lamp %s%s" % (
                p.get("target") or p.get("path"), p.get("power") or "?",
                ", input %s" % p["input"] if p.get("input") else ""))
        elif ports:
            said.append("%d serial adapter%s, none answered" % (
                len(ports), "" if len(ports) == 1 else "s"))
        else:
            said.append("no serial adapter is plugged in")
        text = "; ".join(said)
        return text[0].upper() + text[1:] + ". Check, then Save."

    def _save_file(self):
        if self._doc is None:
            return
        values = {key: self._read_widget(widget, original)
                  for key, (widget, original) in self._widgets.items()}
        try:
            if self._writable:
                for key, value in values.items():
                    self._doc[key] = value
                config_file.save(self._path, self._doc)
                # Written into the file now, so nothing is left to override.
                site_vars.save_overrides({})
                said = "Saved."
            else:
                # Only what differs from the file: a value put back to the
                # file's own stops being an override rather than pinning it.
                changed = {key: value for key, value in values.items()
                           if value != _plain(self._doc.get(key))}
                site_vars.save_overrides(changed)
                said = ("Saved in this app (%d override%s)." % (
                    len(changed), "" if len(changed) == 1 else "s")
                    if changed else "Saved: everything is as installed.")
            site_vars.write_site(self._settings)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        release.forget()
        self._status.setText(said + " The stick has them after its next "
                             "update.")

    @staticmethod
    def _read_widget(widget, original):
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return _value(widget)
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

    # --- deploy console ----------------------------------------------------

    def _build_console_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        note = QLabel(
            "Runs Ansible against the stick from the playstick checkout. Run "
            "all applies the whole playbook, as Update device on the Device "
            "page does, and asks for the stick's sudo password first. The "
            "other presets need no password.")
        note.setWordWrap(True)
        layout.addWidget(note)

        presets = QHBoxLayout()
        run_all = QPushButton("Run all")
        run_all.setObjectName("primary")
        run_all.setToolTip("ansible-playbook site.yml: everything a deploy "
                           "does, whether or not the stick is up to date")
        run_all.clicked.connect(self.runAllRequested)
        presets.addWidget(run_all)
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
