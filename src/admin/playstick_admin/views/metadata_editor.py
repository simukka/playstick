"""Edit one film's metadata and correct its match.

Two ways to fix a film here. Type a value into a field and it is pinned,
winning over whatever the preparer derived. Or search TMDb, pick the right
film, and its id is pinned -- the next prepare fetches that exact film and
skips the search and the confidence gate that chose wrong.

WHERE AN EDIT GOES, WHICH IS THE PART THAT CHANGED

Both, and in that order, because the two things that need to know are on
different machines and neither can wait for the other:

  the device, over /api/admin/item, so the change is on the children's grid
  within a second rather than after the next prep run; and

  playstick-overrides.json here, so the next prep run honours it rather than
  deriving the old answer from the filename again.

It used to write only the second, and the Device tab wrote only the first, and
nothing carried either to the other. Writing both from one Save is what makes
"the title is wrong" a thing you fix once. A device that is not answering is
reported and the local file is still written -- the edit is not lost, it is
just not on the stick yet, and the next full sync carries it.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..core import overrides as overrides_mod
from ..core.device_client import DeviceClient, DeviceError
from ..bridges import tmdb_worker

# The fields the daemon will accept on /api/admin/item. Everything else the
# form gathers -- plot, tmdb_id, source_rel -- is the preparer's business and
# the device has no column for it.
DEVICE_FIELDS = ("title", "sort_title", "year", "rating", "genres", "hidden")


class MetadataEditor(QWidget):
    savedOverride = Signal()
    reprepareRequested = Signal(dict)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._movie = None
        self._pending_tmdb_id = None
        self._search_thread = None
        self._build()
        self.set_movie(None)

    def _build(self):
        outer = QVBoxLayout(self)
        self._heading = QLabel("No film selected")
        self._heading.setStyleSheet("font-weight: bold;")
        outer.addWidget(self._heading)

        form = QFormLayout()
        self._title = QLineEdit()
        self._sort_title = QLineEdit()
        self._year = QLineEdit()
        self._rating = QLineEdit()
        self._genres = QLineEdit()
        self._genres.setPlaceholderText("comma, separated")
        self._plot = QPlainTextEdit()
        self._plot.setMaximumHeight(80)
        self._hidden = QCheckBox("Hide from the phone shelf")
        form.addRow("Title", self._title)
        form.addRow("Sort as", self._sort_title)
        form.addRow("Year", self._year)
        form.addRow("Rating", self._rating)
        form.addRow("Genres", self._genres)
        form.addRow("Plot", self._plot)
        form.addRow("", self._hidden)
        outer.addLayout(form)

        self._match_label = QLabel("")
        self._match_label.setWordWrap(True)
        outer.addWidget(self._match_label)

        buttons = QHBoxLayout()
        self._save_btn = QPushButton("Save overrides")
        self._save_btn.clicked.connect(self._save)
        self._clear_btn = QPushButton("Clear overrides")
        self._clear_btn.clicked.connect(self._clear)
        self._reprep_btn = QPushButton("Save && re-prepare")
        self._reprep_btn.clicked.connect(self._save_and_reprepare)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._clear_btn)
        buttons.addWidget(self._reprep_btn)
        outer.addLayout(buttons)

        tmdb_box = QGroupBox("Correct the match (TMDb)")
        box = QVBoxLayout(tmdb_box)
        query_row = QHBoxLayout()
        self._query = QLineEdit()
        self._query.setPlaceholderText("Search title")
        self._query.returnPressed.connect(self._search)
        self._query_year = QLineEdit()
        self._query_year.setPlaceholderText("Year")
        self._query_year.setMaximumWidth(70)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._search)
        query_row.addWidget(self._query, 1)
        query_row.addWidget(self._query_year)
        query_row.addWidget(self._search_btn)
        box.addLayout(query_row)
        self._results = QListWidget()
        self._results.itemDoubleClicked.connect(self._use_selected)
        box.addWidget(self._results)
        self._use_btn = QPushButton("Use selected as the match")
        self._use_btn.clicked.connect(self._use_selected)
        box.addWidget(self._use_btn)
        outer.addWidget(tmdb_box, 1)

    def set_movie(self, movie):
        self._movie = movie
        self._pending_tmdb_id = None
        enabled = movie is not None
        for w in (self._title, self._sort_title, self._year, self._rating,
                  self._genres, self._plot, self._hidden, self._save_btn,
                  self._clear_btn, self._reprep_btn, self._query,
                  self._query_year, self._search_btn, self._results,
                  self._use_btn):
            w.setEnabled(enabled)
        self._results.clear()
        if not movie:
            self._heading.setText("No film selected")
            for w in (self._title, self._sort_title, self._year, self._rating,
                      self._genres, self._query, self._query_year):
                w.clear()
            self._plot.clear()
            self._hidden.setChecked(False)
            self._match_label.setText("")
            return
        self._heading.setText(movie.get("source_rel") or movie.get("title") or "")
        self._title.setText(str(movie.get("title") or ""))
        self._sort_title.setText(str(movie.get("sort_title") or ""))
        self._year.setText(str(movie.get("year") or ""))
        self._rating.setText(str(movie.get("rating") or ""))
        genres = movie.get("genres") or []
        self._genres.setText(", ".join(genres) if isinstance(genres, list) else str(genres))
        self._plot.setPlainText(str(movie.get("plot") or ""))
        self._hidden.setChecked(bool(movie.get("hidden")))
        self._query.setText(str(movie.get("title") or ""))
        self._query_year.setText(str(movie.get("year") or ""))
        self._update_match_label()

    def _update_match_label(self):
        if self._pending_tmdb_id:
            self._match_label.setText("Match to pin: TMDb #%s" % self._pending_tmdb_id)
        elif self._movie and self._movie.get("override_tmdb_id"):
            self._match_label.setText("Pinned match: TMDb #%s"
                                      % self._movie["override_tmdb_id"])
        elif self._movie and self._movie.get("tmdb_id"):
            self._match_label.setText("Automatic match: TMDb #%s (score %s)"
                                      % (self._movie["tmdb_id"],
                                         self._movie.get("tmdb_score", "?")))
        else:
            self._match_label.setText("No TMDb match")

    # --- overrides ---------------------------------------------------------

    def _gather(self):
        year = self._year.text().strip()
        rating = self._rating.text().strip()
        genres = [g.strip() for g in self._genres.text().split(",") if g.strip()]
        fields = {
            "source_rel": self._movie.get("source_rel"),
            "title": self._title.text().strip(),
            "sort_title": self._sort_title.text().strip(),
            "year": int(year) if year.isdigit() else None,
            "rating": _to_float(rating),
            "genres": genres,
            "plot": self._plot.toPlainText().strip(),
            "hidden": True if self._hidden.isChecked() else None,
        }
        if self._pending_tmdb_id:
            fields["tmdb_id"] = self._pending_tmdb_id
        elif self._movie.get("override_tmdb_id"):
            fields["tmdb_id"] = self._movie["override_tmdb_id"]
        return fields

    def _save(self):
        if not self._movie:
            return
        output = self._settings.effective_output()
        if not output:
            QMessageBox.warning(self, "No library", "Set a source library first.")
            return
        fields = self._gather()
        # The local file first, unconditionally: it is the one write that
        # cannot fail for a reason outside this machine, and doing it first
        # means an unreachable stick costs the edit nothing.
        overrides_mod.set_entry(output, self._movie["id"], **fields)
        self._push(self._movie["id"], {k: fields.get(k) for k in DEVICE_FIELDS})
        self.savedOverride.emit()

    def _clear(self):
        if not self._movie:
            return
        output = self._settings.effective_output()
        overrides_mod.clear_entry(output, self._movie["id"])
        # None on every field is how the daemon spells "forget this edit and go
        # back to what the sync said" -- the same reset the form's empty boxes
        # mean here. Clearing locally without this would leave the device still
        # showing the correction.
        self._push(self._movie["id"], {k: None for k in DEVICE_FIELDS})
        self._pending_tmdb_id = None
        self.savedOverride.emit()

    def _push(self, film_id, fields):
        """The same edit, onto the device.

        Synchronous, unlike everything in the Device tab, and deliberately: this
        is one small POST on a LAN behind a button somebody just pressed, and a
        worker thread here would mean Save returning before the grid changed --
        which is the one thing this button is for. The client's own timeout
        bounds it.
        """
        url = self._settings.device_url
        if not url:
            return
        try:
            DeviceClient(url).admin_item(film_id, fields)
        except DeviceError as exc:
            QMessageBox.warning(
                self, "Saved here, not on the device",
                "The edit is in the overrides file and the next prep run will "
                "use it, but the stick did not answer:\n\n%s" % exc)

    def _save_and_reprepare(self):
        if not self._movie:
            return
        movie = self._movie
        self._save()
        self.reprepareRequested.emit(movie)

    # --- TMDb search -------------------------------------------------------

    def _search(self):
        key = self._settings.tmdb_key
        if not key:
            QMessageBox.information(self, "No TMDb key",
                                    "Set a TMDb API key in Settings to search.")
            return
        query = self._query.text().strip()
        if not query:
            return
        year = self._query_year.text().strip()
        year = int(year) if year.isdigit() else None
        self._results.clear()
        self._results.addItem("Searching...")
        self._search_btn.setEnabled(False)
        job, thread = tmdb_worker.start_search(
            key, self._settings.effective_output(),
            self._settings.tmdb_language, query, year)
        self._search_thread = thread          # keep a reference alive
        job.results.connect(self._on_results)
        job.failed.connect(self._on_search_failed)
        thread.start()

    def _on_results(self, rows):
        self._search_btn.setEnabled(True)
        self._results.clear()
        if not rows:
            self._results.addItem("No results")
            return
        for row in rows:
            label = "%s (%s)" % (row["title"], row.get("year") or "?")
            if row.get("rating"):
                label += "  \u2605 %s" % row["rating"]
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, row)
            item.setToolTip(row.get("overview") or "")
            self._results.addItem(item)

    def _on_search_failed(self, message):
        self._search_btn.setEnabled(True)
        self._results.clear()
        self._results.addItem("Search failed: %s" % message)

    def _use_selected(self):
        item = self._results.currentItem()
        row = item.data(Qt.UserRole) if item else None
        if not isinstance(row, dict):
            return
        self._pending_tmdb_id = row.get("id")
        # Fill the visible fields from the chosen candidate so a save without a
        # re-prepare still corrects the shelf immediately.
        if row.get("title"):
            self._title.setText(row["title"])
        if row.get("year"):
            self._year.setText(str(row["year"]))
        if row.get("rating"):
            self._rating.setText(str(row["rating"]))
        if row.get("overview"):
            self._plot.setPlainText(row["overview"])
        self._update_match_label()


def _to_float(text):
    try:
        return round(float(text), 1)
    except (TypeError, ValueError):
        return None
