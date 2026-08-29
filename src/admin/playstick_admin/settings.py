"""Persistent application settings, backed by QSettings.

Everything a run needs that is not in the library itself: where the source
films are, where prepared output goes, how to reach the stick, and the optional
TMDb key. Stored per-user by Qt in the platform's native location.
"""

from PySide6.QtCore import QSettings

from . import paths

ORG = "playstick"
APP = "admin"


class Settings:
    def __init__(self):
        self._s = QSettings(ORG, APP)

    def _get(self, key, default=""):
        value = self._s.value(key, default)
        return value if value is not None else default

    def _set(self, key, value):
        self._s.setValue(key, value)
        self._s.sync()

    # The library the preparer reads (the source films).
    @property
    def source_library(self):
        return str(self._get("source_library", ""))

    @source_library.setter
    def source_library(self, value):
        self._set("source_library", value)

    # Where the index and .playstick/ are written. Empty means "the library".
    @property
    def output_dir(self):
        return str(self._get("output_dir", ""))

    @output_dir.setter
    def output_dir(self, value):
        self._set("output_dir", value)

    def effective_output(self):
        return self.output_dir or self.source_library

    # Base URL of the running daemon on the stick, e.g. http://vivostick.local
    @property
    def device_url(self):
        return str(self._get("device_url", "")).rstrip("/")

    @device_url.setter
    def device_url(self, value):
        self._set("device_url", value)

    @property
    def tmdb_key(self):
        return str(self._get("tmdb_key", ""))

    @tmdb_key.setter
    def tmdb_key(self, value):
        self._set("tmdb_key", value)

    @property
    def tmdb_language(self):
        return str(self._get("tmdb_language", "en-US"))

    @tmdb_language.setter
    def tmdb_language(self, value):
        self._set("tmdb_language", value)

    @property
    def ffmpeg(self):
        return str(self._get("ffmpeg", ""))

    @ffmpeg.setter
    def ffmpeg(self, value):
        self._set("ffmpeg", value)

    @property
    def ffprobe(self):
        return str(self._get("ffprobe", ""))

    @ffprobe.setter
    def ffprobe(self, value):
        self._set("ffprobe", value)

    @property
    def repo_root(self):
        return str(self._get("repo_root", paths.REPO_ROOT))

    @repo_root.setter
    def repo_root(self, value):
        self._set("repo_root", value)

    # Extra flags appended to every preparer run, as a single string.
    @property
    def prep_extra_args(self):
        return str(self._get("prep_extra_args", ""))

    @prep_extra_args.setter
    def prep_extra_args(self, value):
        self._set("prep_extra_args", value)
