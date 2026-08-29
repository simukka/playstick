"""Interactive TMDb search for the metadata editor.

Wraps playstick_prep.Tmdb so a human can search by hand and pick the right film,
which is a different question from the preparer's automatic lookup: here there is
no confidence gate, because the person choosing is the confidence. Shares the
same on-disk cache as a prep run, so a title looked at here is free there.
"""

import os

from .. import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402


class TmdbError(Exception):
    pass


class TmdbSearch:
    def __init__(self, key, output_dir, language="en-US"):
        if not key:
            raise TmdbError("no TMDb key configured")
        cache = os.path.join(output_dir, prep.WORK_DIR, "tmdb-cache.json")
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        self._client = prep.Tmdb(key, cache, language)

    def search(self, query, year=None):
        """Raw candidates for a query, best-known first, mapped to plain rows
        the UI can show without knowing anything about TMDb."""
        results = self._client.candidates(query, year) if query else []
        rows = []
        for item in results:
            release = (item.get("release_date") or "")[:4]
            rows.append({
                "id": item.get("id"),
                "title": item.get("title") or item.get("original_title") or "?",
                "year": int(release) if release.isdigit() else None,
                "overview": item.get("overview") or "",
                "rating": item.get("vote_average"),
                "votes": item.get("vote_count"),
                "poster_url": (prep.Tmdb.IMG + item["poster_path"])
                              if item.get("poster_path") else None,
            })
        self._client.save()
        return rows

    def detail(self, tmdb_id):
        """The full metadata the preparer would write for a chosen id."""
        meta, why = self._client.fetch_by_id(tmdb_id)
        self._client.save()
        if why:
            raise TmdbError(why)
        return meta
