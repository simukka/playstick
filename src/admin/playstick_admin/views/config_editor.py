"""The Config tab: edit the Ansible variable files and run actl.

Two sub-panels. A generated form of the top-level tunables in group_vars/all.yml
(and host_vars/vivostick/local.yml), edited in place so comments survive the
round-trip. And an actl console for the commands that need no become password --
a syntax check and a ping -- with a free-form box for anything else, which a
privileged deploy still needs a real terminal for.

Both work on the checkout named by settings.repo_root, read afresh each time,
so that an installed app -- which has no repo of its own -- can be pointed at
one in Settings without a restart. Until it is, the tab says so.
"""

import os

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import paths
from ..core import config_file
from ..bridges.process_runner import ActlRunner

_NO_CHECKOUT = (
    "No playstick checkout to work on (repo root: %s).\n\n"
    "This tab edits the repo's group_vars and runs its actl, so it needs a "
    "clone of the playstick repo. Set \"Repo root\" in Settings to one.")


class ConfigTab(QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._doc = None
        self._path = ""
        self._widgets = {}          # key -> (widget, original_value)
        self._runner = ActlRunner(self)
        self._runner.output.connect(self._append_console)
        self._runner.finished.connect(lambda _c: self._set_running(False))
        self._build()
        self._reload_file()

    def _build(self):
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_vars_tab(), "Variables")
        tabs.addTab(self._build_console_tab(), "Deploy / actl")
        outer.addWidget(tabs)

    # --- variables ---------------------------------------------------------

    def _build_vars_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        top = QHBoxLayout()
        self._file_pick = QComboBox()
        self._file_pick.addItem("group_vars/all.yml", paths.group_vars)
        self._file_pick.addItem("host_vars/vivostick/local.yml", paths.host_vars)
        self._file_pick.currentIndexChanged.connect(self._reload_file)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._reload_file)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_file)
        top.addWidget(QLabel("File:"))
        top.addWidget(self._file_pick, 1)
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
        """Re-read the file, for when Settings may have named another repo."""
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
        self._path = self._file_pick.currentData()(root)
        if not self._path or not os.path.isfile(self._path):
            self._doc = None
            vbox.addWidget(QLabel("File not found: %s" % self._path))
            if self._path and self._path.endswith("local.yml"):
                vbox.addWidget(QLabel("Copy local.yml.example to local.yml first."))
            self._form_host = host
            self._form_area.setWidget(host)
            return
        if not config_file.HAVE_RUAMEL:
            self._doc = None
            self._status.setText("ruamel.yaml is not installed - showing read-only "
                                 "text. Run: pip install ruamel.yaml")
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
            vbox.addWidget(QLabel("Could not parse: %s" % exc))
            self._form_host = host
            self._form_area.setWidget(host)
            return
        self._status.setText("Editing %s - comments are preserved on save."
                             % os.path.basename(self._path))
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
        label = QLabel("(complex value - edit in the file directly)")
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
        self._status.setText("Saved %s." % os.path.basename(self._path))

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
            "Runs ./actl in the repo. The presets need no password. A real "
            "deploy uses ansible -K, which needs a terminal - run "
            "\"./actl 'ansible-playbook site.yml -K'\" there.")
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
        run_btn.clicked.connect(lambda: self._run(self._cmd.text().strip()))
        cmd_row.addWidget(self._cmd, 1)
        cmd_row.addWidget(run_btn)
        layout.addLayout(cmd_row)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(10000)
        layout.addWidget(self._console, 1)
        return page

    def _run(self, command):
        if self._runner.is_running():
            return
        root = self._settings.repo_root
        if not paths.is_checkout(root):
            self._append_console(_NO_CHECKOUT % (root or "not set") + "\n")
            return
        self._set_running(True)
        self._runner.run(root, command)

    def _set_running(self, running):
        self._stop_btn.setEnabled(running)

    def _append_console(self, text):
        cursor = self._console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self._console.setTextCursor(cursor)
