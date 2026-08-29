"""Application settings dialog."""

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLineEdit, QPushButton,
                               QWidget)


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


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        form = QFormLayout(self)

        self._source = _PathRow(settings.source_library, pick_dir=True)
        self._output = _PathRow(settings.output_dir, pick_dir=True)
        self._device = QLineEdit(settings.device_url)
        self._device.setPlaceholderText("http://vivostick.local")
        self._tmdb = QLineEdit(settings.tmdb_key)
        self._tmdb_lang = QLineEdit(settings.tmdb_language)
        self._ffmpeg = _PathRow(settings.ffmpeg, pick_dir=False)
        self._ffprobe = _PathRow(settings.ffprobe, pick_dir=False)
        self._repo = _PathRow(settings.repo_root, pick_dir=True)
        self._extra = QLineEdit(settings.prep_extra_args)
        self._extra.setPlaceholderText("--transcode never --verify probe")

        form.addRow("Source library", self._source)
        form.addRow("Output (blank = library)", self._output)
        form.addRow("Device URL", self._device)
        form.addRow("TMDb API key", self._tmdb)
        form.addRow("TMDb language", self._tmdb_lang)
        form.addRow("ffmpeg (optional)", self._ffmpeg)
        form.addRow("ffprobe (optional)", self._ffprobe)
        form.addRow("Repo root (for actl)", self._repo)
        form.addRow("Extra prepare flags", self._extra)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        s = self._settings
        s.source_library = self._source.edit.text().strip()
        s.output_dir = self._output.edit.text().strip()
        s.device_url = self._device.text().strip()
        s.tmdb_key = self._tmdb.text().strip()
        s.tmdb_language = self._tmdb_lang.text().strip() or "en-US"
        s.ffmpeg = self._ffmpeg.edit.text().strip()
        s.ffprobe = self._ffprobe.edit.text().strip()
        s.repo_root = self._repo.edit.text().strip()
        s.prep_extra_args = self._extra.text().strip()
        self.accept()
