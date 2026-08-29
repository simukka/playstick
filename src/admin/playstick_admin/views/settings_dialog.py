"""Application settings dialog.

The one row worth a comment is the NAS root, which is checked for writability
the moment it is set rather than at the moment a job fails. The stick mounts the
share read-only and this machine is the one that puts films on it, so a
read-only mount here produces a working scan, a working encode, and a failure at
the last step -- an hour later, every time. That is the same shape of invisible
fault as the overrides file that used to live on a read-only mount and silently
lost every correction anybody made.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QWidget)

from .. import paths


class _PathRow(QWidget):
    def __init__(self, initial="", pick_dir=True):
        super().__init__()
        self._pick_dir = pick_dir
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(initial)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self):
        if self._pick_dir:
            path = QFileDialog.getExistingDirectory(self, "Choose folder",
                                                    self.edit.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose file",
                                                  self.edit.text())
        if path:
            self.edit.setText(path)


class _Probe:
    """Just enough of Settings to reuse its check against unsaved text."""

    def __init__(self, nas_root):
        self.nas_root = nas_root

    def problem(self):
        from ..settings import Settings
        return Settings.nas_root_problem(self)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        form = QFormLayout(self)

        self._nas = _PathRow(settings.nas_root, pick_dir=True)
        self._nas.edit.textChanged.connect(self._check_nas)
        self._nas_note = QLabel("")
        self._nas_note.setWordWrap(True)
        self._staging = _PathRow(settings.staging_dir, pick_dir=True)
        self._device = QLineEdit(settings.device_url)
        self._device.setPlaceholderText("http://vivostick.local")
        self._tmdb = QLineEdit(settings.tmdb_key)
        self._tmdb_lang = QLineEdit(settings.tmdb_language)
        self._ffmpeg = _PathRow(settings.ffmpeg, pick_dir=False)
        self._ffprobe = _PathRow(settings.ffprobe, pick_dir=False)
        self._repo = _PathRow(settings.repo_root, pick_dir=True)
        self._extra = QLineEdit(settings.prep_extra_args)
        self._extra.setPlaceholderText("--transcode never --verify probe")

        form.addRow("NAS root (the share, as mounted here)", self._nas)
        form.addRow("", self._nas_note)
        form.addRow("Staging (films are made here first)", self._staging)
        form.addRow("Device URL", self._device)
        form.addRow("TMDb API key", self._tmdb)
        form.addRow("TMDb language", self._tmdb_lang)
        form.addRow("ffmpeg (optional)", self._ffmpeg)
        form.addRow("ffprobe (optional)", self._ffprobe)
        form.addRow("Repo root (for actl)", self._repo)
        form.addRow("Extra prepare flags", self._extra)

        where = QLabel(paths.db_path())
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        where.setToolTip(
            "Sources, what has been found, what is prepared and every "
            "metadata correction. Not on the share, because none of it is "
            "anything the stick reads.")
        form.addRow("This machine's library", where)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _check_nas(self, text):
        """Say what is wrong with this share now, not in an hour."""
        probe = _Probe(text.strip())
        problem = probe.problem()
        self._nas_note.setText(problem or "")
        self._nas_note.setStyleSheet("color: #b00;" if problem else "")

    def _save(self):
        s = self._settings
        s.nas_root = self._nas.edit.text().strip()
        s.staging_dir = self._staging.edit.text().strip()
        s.device_url = self._device.text().strip()
        s.tmdb_key = self._tmdb.text().strip()
        s.tmdb_language = self._tmdb_lang.text().strip() or "en-US"
        s.ffmpeg = self._ffmpeg.edit.text().strip()
        s.ffprobe = self._ffprobe.edit.text().strip()
        s.repo_root = self._repo.edit.text().strip()
        s.prep_extra_args = self._extra.text().strip()
        self.accept()
