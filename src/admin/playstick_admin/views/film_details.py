"""One film, everything about it: the window a double-click in the Prepare tab
opens.

Read-only on purpose. Correcting a film is the Library tab's job, and a second
place to edit the same fields would be a second place for them to disagree.

The sections are trees of whatever core.film_details gathered rather than forms
laid out field by field, so a column added to the schema shows up here without
anybody remembering this window exists. ffprobe is run when the window opens,
on a worker thread: a file on a sleeping mount can take seconds to answer, and
the rest of what is known about the film should not wait for it. TMDb's
poster, plot and rating at the top come the same way, for the same reason.
The Frames tab is views.film_frames.
"""

import datetime
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QStackedWidget,
                               QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import format as fmt
from ..bridges import net_worker
from ..core import film_details
from .film_frames import FramesTab

POSTER_WIDTH = 154

# Columns that hold a unix time, shown as one rather than as 1759497965.27.
_TIMES = {"seen_at", "mtime", "prepared_at", "pushed_at", "queued_at",
          "started_at", "finished_at", "updated_at", "scanned_at"}
_SIZES = {"size", "bytes_on_nas", "bytes"}


def _scalar(key, value):
    if value is None:
        return ""
    if key in _TIMES and isinstance(value, (int, float)) and value:
        return datetime.datetime.fromtimestamp(value).strftime(
            "%Y-%m-%d %H:%M:%S")
    if key in _SIZES and isinstance(value, int) and value:
        return "%s  (%d bytes)" % (fmt.human_size(value), value)
    if isinstance(value, str) and "\n" in value:
        # A job log is hundreds of lines; the tree shows the first, the
        # tooltip and the copy button have the rest.
        first = value.strip().splitlines()[0] if value.strip() else ""
        return "%s  [%d lines]" % (first, value.count("\n") + 1)
    return str(value)


def _fill(parent, value):
    """Add `value` under `parent`, a tree item, recursively."""
    if isinstance(value, dict):
        for key, child in value.items():
            _add(parent, str(key), child)
    elif isinstance(value, list):
        for n, child in enumerate(value):
            _add(parent, "[%d]" % n, child)


def _add(parent, key, value):
    item = QTreeWidgetItem(parent, [key])
    if isinstance(value, (dict, list)):
        item.setText(1, "{%d}" % len(value) if isinstance(value, dict)
                     else "[%d]" % len(value))
        _fill(item, value)
    else:
        item.setText(1, _scalar(key, value))
        if isinstance(value, str) and len(value) > 80:
            item.setToolTip(1, value)
    return item


class _Section(QWidget):
    """A tree of one section, with a button to copy it as JSON -- which is what
    anybody pasting it into a bug report actually wants."""

    def __init__(self, empty_text):
        super().__init__()
        self._value = None
        layout = QVBoxLayout(self)
        self._stack = QStackedWidget()
        self._message = QLabel(empty_text)
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Field", "Value"])
        self._tree.setColumnWidth(0, 220)
        self._tree.setUniformRowHeights(True)
        self._raw = QPlainTextEdit()
        self._raw.setReadOnly(True)
        self._raw.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._stack.addWidget(self._message)
        self._stack.addWidget(self._tree)
        self._stack.addWidget(self._raw)
        layout.addWidget(self._stack, 1)

        buttons = QHBoxLayout()
        self._raw_btn = QPushButton("Show JSON")
        self._raw_btn.setCheckable(True)
        self._raw_btn.toggled.connect(self._show_raw)
        self._copy_btn = QPushButton("Copy JSON")
        self._copy_btn.clicked.connect(self._copy)
        buttons.addStretch(1)
        buttons.addWidget(self._raw_btn)
        buttons.addWidget(self._copy_btn)
        layout.addLayout(buttons)
        self._set_buttons(False)

    def show_message(self, text):
        self._value = None
        self._message.setText(text)
        self._raw_btn.setChecked(False)
        self._stack.setCurrentWidget(self._message)
        self._set_buttons(False)

    def show_value(self, value, expand=1):
        if not value:
            return
        self._value = value
        self._tree.clear()
        _fill(self._tree.invisibleRootItem(), value)
        if expand:
            self._tree.expandToDepth(expand - 1)
        self._tree.resizeColumnToContents(0)
        self._raw.setPlainText(self._json())
        self._stack.setCurrentWidget(
            self._raw if self._raw_btn.isChecked() else self._tree)
        self._set_buttons(True)

    def _json(self):
        return json.dumps(self._value, indent=2, ensure_ascii=False,
                          default=str)

    def _set_buttons(self, on):
        self._raw_btn.setEnabled(on)
        self._copy_btn.setEnabled(on)

    def _show_raw(self, on):
        if self._value is not None:
            self._stack.setCurrentWidget(self._raw if on else self._tree)

    def _copy(self):
        if self._value is not None:
            QGuiApplication.clipboard().setText(self._json())


class FilmDetailsDialog(QDialog):
    def __init__(self, db, settings, ident, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._db = db
        self._settings = settings
        self._ident = ident
        self._abs_path = None
        self.resize(860, 780)

        layout = QVBoxLayout(self)
        self._heading = QLabel()
        self._heading.setTextFormat(Qt.RichText)
        self._heading.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._heading.setWordWrap(True)
        layout.addWidget(self._heading)
        layout.addWidget(self._build_tmdb())

        self._tabs = QTabWidget()
        self._item = _Section("Nothing is recorded for this film.")
        self._prepared = _Section(
            "Not prepared yet. What the preparer decided about it -- title, "
            "plot, genres, the tracks it kept -- appears here once it has "
            "been through the queue.")
        self._overrides = _Section("Nobody has corrected anything about it.")
        self._probe = _Section(
            "The preparer has not probed it yet. A scan reads only the "
            "filename; the probe happens when the film is first prepared.")
        self._ffprobe = _Section("Running ffprobe...")
        self._jobs = _Section("It has never been queued.")
        self._frames = FramesTab()
        self._tabs.addTab(self._item, "Catalogue")
        self._tabs.addTab(self._prepared, "Prepared")
        self._tabs.addTab(self._overrides, "Corrections")
        self._tabs.addTab(self._probe, "Preparer's probe")
        self._tabs.addTab(self._ffprobe, "ffprobe")
        self._tabs.addTab(self._jobs, "Jobs")
        self._tabs.addTab(self._frames, "Frames")
        self._tabs.currentChanged.connect(self._on_tab)
        layout.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self._reprobe = buttons.addButton("Run ffprobe again",
                                          QDialogButtonBox.ActionRole)
        self._reprobe.clicked.connect(self._run_ffprobe)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._details = None
        self._load()
        self._run_ffprobe()
        self._run_tmdb()

    def _build_tmdb(self):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        self._poster = QLabel()
        self._poster.setFixedSize(POSTER_WIDTH, POSTER_WIDTH * 3 // 2)
        self._poster.setAlignment(Qt.AlignCenter)
        self._poster.setStyleSheet("border: 1px solid palette(mid);")
        row.addWidget(self._poster, 0, Qt.AlignTop)
        text = QVBoxLayout()
        self._tmdb_title = QLabel()
        self._tmdb_title.setTextFormat(Qt.RichText)
        self._tmdb_title.setWordWrap(True)
        self._tmdb_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._plot = QLabel()
        self._plot.setWordWrap(True)
        self._plot.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._plot.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(self._tmdb_title)
        text.addWidget(self._plot, 1)
        row.addLayout(text, 1)
        return box

    def _load(self):
        details = film_details.gather(self._db, self._ident)
        if details is None:
            self.setWindowTitle("Film")
            self._heading.setText("This film is no longer in the catalogue.")
            self._reprobe.setEnabled(False)
            self._ffprobe.show_message("")
            return
        self._details = details
        item = details["item"]
        self._abs_path = item["abs_path"]
        self._frames.set_duration(item["duration"])
        title = (details["prepared"] or {}).get("title") or item["title"] \
            or item["rel"]
        year = (details["prepared"] or {}).get("year") or item["year"]
        name = "%s (%s)" % (title, year) if year else title
        self.setWindowTitle(name)
        self._frames.set_film(item["abs_path"], self._settings.ffmpeg, name)
        self._heading.setText(
            "<b>%s</b><br><small>%s<br>id %s &middot; %s</small>"
            % (_esc(name), _esc(item["abs_path"]), _esc(item["id"]),
               _esc(item["state"])))

        self._item.show_value(item)
        prepared = dict(details["prepared"] or {})
        if prepared and details["poster"]:
            prepared["poster"] = details["poster"]
        self._prepared.show_value(prepared)
        self._overrides.show_value(details["overrides"])
        self._probe.show_value(details["probe"])
        self._jobs.show_value(details["jobs"], expand=2)

    def _run_ffprobe(self):
        if not self._abs_path:
            return
        self._reprobe.setEnabled(False)
        self._ffprobe.show_message("Running ffprobe on %s..." % self._abs_path)
        path, binary = self._abs_path, self._settings.ffprobe
        job, thread = net_worker.call(
            lambda: film_details.ffprobe(path, binary), label="ffprobe")
        job.done.connect(self._on_ffprobe)
        job.failed.connect(self._on_ffprobe_failed)
        # Methods rather than lambdas: Qt drops a connection to a method when
        # its object is deleted, and this window is deleted when it closes --
        # which can be long before a probe of a sleeping mount comes back.
        thread.finished.connect(self._on_ffprobe_finished)
        thread.start()

    def _on_ffprobe(self, info):
        self._ffprobe.show_value(info, expand=2)
        try:
            self._frames.set_duration(float(info["format"]["duration"]))
        except (KeyError, TypeError, ValueError):
            pass

    def _on_tab(self, index):
        if self._tabs.widget(index) is self._frames:
            self._frames.start()

    def _on_ffprobe_finished(self):
        self._reprobe.setEnabled(True)

    def _on_ffprobe_failed(self, message):
        self._ffprobe.show_message("ffprobe could not read it:\n\n%s" % message)

    def _run_tmdb(self):
        self._poster.setText("No poster")
        if self._details is None:
            self._tmdb_title.setText("")
            return
        key, language = self._settings.tmdb_key, self._settings.tmdb_language
        if not key:
            self._tmdb_title.setText(
                "<i>No TMDb key configured -- Settings, TMDb API key.</i>")
            return
        self._tmdb_title.setText("<i>Asking TMDb...</i>")
        details = self._details
        job, thread = net_worker.call(
            lambda: film_details.tmdb_preview(key, language, details),
            label="tmdb")
        job.done.connect(self._on_tmdb)
        job.failed.connect(self._on_tmdb_failed)
        thread.start()

    def _on_tmdb(self, info):
        if info["why"]:
            self._tmdb_title.setText(
                "<i>TMDb has no confident match: %s</i>" % _esc(info["why"]))
            return
        name = info["title"] or "?"
        if info["year"]:
            name = "%s (%s)" % (name, info["year"])
        facts = []
        if info["rating"]:
            facts.append("&#9733; %s / 10" % info["rating"])
            if info["votes"]:
                facts[-1] += " (%s votes)" % format(info["votes"], ",")
        if info["genres"]:
            facts.append(_esc(", ".join(info["genres"])))
        facts.append("TMDb #%s, %s" % (_esc(info["tmdb_id"]), info["how"]))
        self._tmdb_title.setText("<b>%s</b><br><small>%s</small>"
                                 % (_esc(name), " &middot; ".join(facts)))
        self._plot.setText(info["plot"] or "TMDb has no description of it.")
        pixmap = QPixmap()
        if info["poster"] and pixmap.loadFromData(info["poster"]):
            self._poster.setPixmap(pixmap.scaled(
                self._poster.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def _on_tmdb_failed(self, message):
        self._tmdb_title.setText("<i>TMDb could not be asked: %s</i>"
                                 % _esc(message))


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
