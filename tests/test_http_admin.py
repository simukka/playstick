"""The routes the desktop admin app uses, and the boundary they defend.

These matter more than the rest of the HTTP surface, because they are the only
routes on this daemon that accept a path in any form. Every phone-facing route
takes an opaque id and looks it up in a table; /api/admin/films takes a
library-relative path and writes it into that table, which is the one place
where what a client sent becomes something that will later be opened.

So the first class below is the whole reason this file exists. The rest is the
shape of a sync: what it writes, what it refuses, and what it must leave alone.

Unlike test_http_api, these run against a REAL Library and Thumbs on a scratch
database. A fake would make the traversal tests meaningless -- the check being
tested is the one between the request and the row, and a fake has no rows.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import ApiTest, LIBRARY_DIR, real_store, api   # noqa: E402


FILM = "0123456789abcdef"
OTHER = "fedcba9876543210"
JPEG = b"\xff\xd8\xff\xe0" + b"body" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"body" * 8


def entry(ident=FILM, rel="Ponyo/Ponyo.mkv", **extra):
    payload = {"id": ident, "source_rel": rel, "title": "Ponyo"}
    payload.update(extra)
    return payload


class AdminTest(ApiTest):
    """ApiTest, with the real store swapped in for FakeLibrary/FakeThumbs."""

    def setUp(self):
        super().setUp()
        self.db, self.real_library, self.real_thumbs = real_store("http")
        # The handler is a throwaway subclass per test (see ApiTest), so
        # replacing its class attributes here cannot leak into another test.
        handler = self.server.RequestHandlerClass
        handler.library = self.real_library
        handler.thumbs = self.real_thumbs
        handler.db = self.db

    def sync(self, films, reconcile=False, status=200):
        resp = self.fetch("/api/admin/films", method="POST",
                          body={"films": films, "reconcile": reconcile})
        self.assertEqual(resp.status, status, resp.body)
        return resp.json

    def put_thumb(self, ident, data, ctype="image/jpeg"):
        return self.fetch("/api/admin/thumb/" + ident, method="PUT",
                          body=data, headers={"Content-Type": ctype})


class NoPathEscapesTheLibrary(AdminTest):
    """The one boundary this endpoint moved, and every way of pushing on it.

    The package docstring used to say no filesystem path ever crosses the HTTP
    boundary. It now says something narrower and true, and this is the class
    that holds it to the narrower thing: a path may cross, and it may only ever
    name something inside the library root.
    """

    def test_a_traversal_is_refused_with_a_sentence(self):
        """Refused, not skipped. A film silently dropped for a bad path
        presents as one that mysteriously never appears on the grid, which is
        the failure this endpoint most needs not to have."""
        resp = self.fetch("/api/admin/films", method="POST",
                          body={"films": [entry(rel="../../etc/passwd")]})
        self.assertEqual(resp.status, 400)
        self.assertIn("inside the library", resp.json["error"])
        self.assertEqual(self.real_library.snapshot()[0], [])

    def test_an_absolute_path_is_refused(self):
        """Even one that happens to be inside the library. The rule is that
        these are RELATIVE, so there is no case where an absolute path is the
        right thing and therefore none where it has to be reasoned about."""
        resp = self.fetch(
            "/api/admin/films", method="POST",
            body={"films": [entry(rel=os.path.join(LIBRARY_DIR, "Ponyo.mkv"))]})
        self.assertEqual(resp.status, 400)

    def test_a_traversal_in_a_subtitle_is_refused(self):
        """Every path in the payload, not just the film's. A subtitle rel is
        handed to mpv on the command line."""
        resp = self.fetch("/api/admin/films", method="POST", body={"films": [
            entry(subtitles=[{"rel": "../../../etc/shadow", "lang": "eng"}])]})
        self.assertEqual(resp.status, 400)
        self.assertIn("subtitle", resp.json["error"])

    def test_a_traversal_in_an_audio_track_is_refused(self):
        resp = self.fetch("/api/admin/films", method="POST", body={"films": [
            entry(audio=[{"n": 1, "rel": "../../etc/passwd", "lang": "eng"}])]})
        self.assertEqual(resp.status, 400)
        self.assertIn("audio", resp.json["error"])

    def test_a_symlink_out_of_the_library_is_refused(self):
        """realpath, not string arithmetic. A relative path with no ".." in it
        can still leave the share if something on the share points out of it,
        and a check that only looked at the text would pass this."""
        target = os.path.join(self.escape_root(), "secret.mkv")
        link = os.path.join(LIBRARY_DIR, "escape.mkv")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(target, link)
        self.addCleanup(os.unlink, link)
        resp = self.fetch("/api/admin/films", method="POST",
                          body={"films": [entry(rel="escape.mkv")]})
        self.assertEqual(resp.status, 400)

    def escape_root(self):
        root = os.path.join(os.path.dirname(LIBRARY_DIR), "not-the-library")
        os.makedirs(root, exist_ok=True)
        return root

    def test_one_bad_film_refuses_the_whole_batch(self):
        """All or nothing, so a sync never leaves half a library. The index
        this replaced had the same property for the same reason."""
        self.fetch("/api/admin/films", method="POST", body={"films": [
            entry(FILM), entry(OTHER, rel="../../etc/passwd")]})
        self.assertEqual(self.real_library.snapshot()[0], [])

    def test_the_error_names_which_film(self):
        """A library of three hundred and one bad path: "Film 0: ..." is the
        difference between fixing it and bisecting the payload."""
        resp = self.fetch("/api/admin/films", method="POST", body={"films": [
            entry(FILM), entry(OTHER, rel="../../etc/passwd")]})
        self.assertTrue(resp.json["error"].startswith("Film 1:"), resp.json)

    def test_an_id_that_is_not_sixteen_hex_is_refused(self):
        """The id is what /api/thumb and /api/audio match on with a regex. One
        that is not that shape would be a row nothing could ever address."""
        for bad in ["", "ABCDEF0123456789", "0123456789abcde", "../../etc",
                    "0123456789abcdef0"]:
            with self.subTest(ident=bad):
                resp = self.fetch("/api/admin/films", method="POST",
                                  body={"films": [entry(bad)]})
                self.assertEqual(resp.status, 400)


class Syncing(AdminTest):
    def test_a_film_arrives_and_the_grid_shows_it(self):
        """End to end: what the admin app POSTs is what a phone then sees."""
        self.assertEqual(self.sync([entry(year=2008)]),
                         {"written": 1, "removed": 0})
        items = self.assertJson(self.fetch("/api/library"))["items"]
        self.assertEqual([i["id"] for i in items], [FILM])
        self.assertEqual(items[0]["title"], "Ponyo")
        self.assertEqual(items[0]["year"], 2008)

    def test_a_missing_media_rel_falls_back_to_the_source(self):
        """A film with no re-encode plays from its original file, which is what
        the preparer means by leaving media_rel out."""
        self.sync([entry()])
        self.assertEqual(self.real_library.rows()[0]["media_rel"],
                         "Ponyo/Ponyo.mkv")

    def test_reconcile_removes_what_is_no_longer_sent(self):
        self.sync([entry(FILM), entry(OTHER, rel="Gone/Gone.mkv")])
        self.assertEqual(self.sync([entry(FILM)], reconcile=True),
                         {"written": 1, "removed": 1})
        self.assertEqual(
            [i["id"] for i in self.assertJson(
                self.fetch("/api/library"))["items"]], [FILM])

    def test_a_film_sent_twice_in_one_batch_is_refused(self):
        """Two rows with one id is not something the upsert can express, and
        silently keeping the last would hide a duplicate in the preparer's
        index rather than reporting it."""
        resp = self.fetch("/api/admin/films", method="POST",
                          body={"films": [entry(FILM), entry(FILM)]})
        self.assertEqual(resp.status, 400)
        self.assertIn("twice", resp.json["error"])

    def test_a_body_that_is_not_a_list_is_refused(self):
        for body in [{}, {"films": "Ponyo"}, {"films": {"id": FILM}}]:
            with self.subTest(body=body):
                self.assertEqual(
                    self.fetch("/api/admin/films", method="POST",
                               body=body).status, 400)

    def test_a_film_that_is_not_an_object_is_refused(self):
        self.assertEqual(
            self.fetch("/api/admin/films", method="POST",
                       body={"films": ["Ponyo"]}).status, 400)

    def test_a_film_with_no_title_is_refused(self):
        """The title is what a child reads off the tile. A blank one is a
        row nobody can pick."""
        resp = self.fetch("/api/admin/films", method="POST",
                          body={"films": [entry(title="   ")]})
        self.assertEqual(resp.status, 400)

    def test_a_nonsense_year_becomes_nothing_rather_than_an_error(self):
        """The same boundary coercion /api/volume does for a delta: a value
        that cannot be a year is absent, not fatal. A whole library must not be
        refused over one film whose .nfo had "MCMXCI" in it."""
        self.sync([entry(year="not a year", rating=None, duration="soon")])
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertIsNone(item["year"])

    def test_a_rating_keeps_its_kind(self):
        self.sync([entry(FILM, rating=7.8),
                   entry(OTHER, rel="Cert/Cert.mkv", rating="PG")])
        by_id = {i["id"]: i for i in
                 self.assertJson(self.fetch("/api/library"))["items"]}
        self.assertEqual(by_id[FILM]["rating"], 7.8)
        self.assertEqual(by_id[OTHER]["rating"], "PG")

    def test_an_absurd_batch_is_refused_before_it_is_parsed(self):
        big = [entry("%016x" % i, rel="f/%d.mkv" % i)
               for i in range(api.ADMIN_SYNC_MAX + 1)]
        resp = self.fetch("/api/admin/films", method="POST",
                          body={"films": big})
        self.assertEqual(resp.status, 400)


class RemovingOneFilm(AdminTest):
    """POST /api/admin/films/delete, and why it is not reconcile.

    The admin app now takes a film out of the library as a deliberate act:
    delete it here, delete its files from the share, drop it back to
    "available" in the Prepare tab. Doing that with a full push and reconcile
    set would phrase the same request as "everything except these", and would
    therefore also delete every film on the stick the sending machine does not
    know about -- another machine's work, or anything synced before that
    machine's database existed. It would look exactly like a successful
    tidy-up afterwards, which is what makes it worth a route of its own.
    """

    def delete(self, ids, status=200):
        resp = self.fetch("/api/admin/films/delete", method="POST",
                          body={"ids": ids})
        self.assertEqual(resp.status, status, resp.body)
        return resp.json

    def test_it_removes_only_what_it_was_asked_for(self):
        self.sync([entry(FILM), entry(OTHER, "Totoro/Totoro.mkv",
                                      title="Totoro")])
        self.assertEqual(self.delete([FILM])["removed"], 1)
        remaining = [row["id"] for row in self.real_library.rows()]
        self.assertEqual(remaining, [OTHER])

    def test_everything_hanging_off_the_film_goes_with_it(self):
        # A resume position inherited by whatever is next given this id would
        # be a film that starts forty minutes in for no reason anybody can see.
        self.sync([entry(FILM, genres=["Animation"])])
        self.put_thumb(FILM, JPEG)
        self.fetch("/api/admin/item", method="POST",
                   body={"id": FILM, "fields": {"title": "Ponyo on the Cliff"}})
        self.db.write().execute(
            "INSERT INTO playback (film_id, position, updated_at) "
            "VALUES (?, 42.0, 0)", (FILM,))

        self.delete([FILM])

        for table in ("thumb", "override", "playback", "film_genre"):
            left = self.db.one(
                "SELECT COUNT(*) AS n FROM %s WHERE film_id = ?" % table,
                (FILM,))["n"]
            self.assertEqual(left, 0, table)

    def test_deleting_a_film_that_is_not_there_is_not_an_error(self):
        self.sync([entry(FILM)])
        self.assertEqual(self.delete([OTHER])["removed"], 0)

    def test_a_bad_id_refuses_the_whole_request(self):
        # Skipping it would make a typo indistinguishable from a film that was
        # not there.
        self.sync([entry(FILM)])
        self.delete([FILM, "../../etc/passwd"], status=400)
        self.assertEqual([row["id"] for row in self.real_library.rows()], [FILM])

    def test_ids_must_be_a_list(self):
        self.delete("everything", status=400)

    def test_the_shelf_closes_up_behind_it(self):
        self.sync([entry("a" * 16, "a.mkv", sort_title="alpha"),
                   entry("b" * 16, "b.mkv", sort_title="mike"),
                   entry("c" * 16, "c.mkv", sort_title="zulu")])
        self.delete(["b" * 16])
        positions = [r["pos"] for r in self.db.query(
            "SELECT pos FROM film ORDER BY pos")]
        self.assertEqual(positions, [0, 1])


class AdminLibraryRead(AdminTest):
    def test_it_reports_paths_the_phone_facing_route_never_sends(self):
        """The admin app diffs against what it sent, which it cannot do against
        the resolved view /api/library returns."""
        self.sync([entry()])
        films = self.assertJson(self.fetch("/api/admin/library"))["films"]
        self.assertEqual(films[0]["source_rel"], "Ponyo/Ponyo.mkv")

    def test_it_reports_poster_etags(self):
        """What lets a re-sync upload only the posters that actually changed."""
        self.sync([entry()])
        tag = self.put_thumb(FILM, JPEG).json["etag"]
        films = self.assertJson(self.fetch("/api/admin/library"))["films"]
        self.assertEqual(films[0]["thumb_etag"], tag)

    def test_it_reports_edits_so_they_can_be_carried_back_to_the_preparer(self):
        """The device is the one home for an edit now. This is the door out of
        it: without this the next prep run would derive the old title from the
        filename again and the correction would be lost at the next sync."""
        self.sync([entry()])
        self.fetch("/api/admin/item", method="POST",
                   body={"id": FILM, "fields": {"title": "Corrected"}})
        films = self.assertJson(self.fetch("/api/admin/library"))["films"]
        self.assertEqual(films[0]["overrides"], {"title": "Corrected"})

    def test_it_is_behind_the_network_check(self):
        with self.settings_allow_nothing():
            self.assertEqual(self.fetch("/api/admin/library").status, 403)

    def settings_allow_nothing(self):
        import ipaddress
        from unittest import mock
        return mock.patch.object(
            api, "ALLOW_NETWORKS", [ipaddress.ip_network("10.99.0.0/16")])


class Posters(AdminTest):
    def test_an_uploaded_poster_is_served_back(self):
        self.sync([entry()])
        put = self.put_thumb(FILM, JPEG)
        self.assertEqual(put.status, 200)
        self.assertEqual(put.json["bytes"], len(JPEG))
        got = self.fetch("/api/thumb/" + FILM)
        self.assertEqual(got.status, 200)
        self.assertEqual(got.body, JPEG)
        self.assertEqual(got.header("Content-Type"), "image/jpeg")

    def test_the_etag_it_returns_is_the_one_that_gets_a_304(self):
        """The two halves of the same number: what the admin app compares
        against, and what a phone revalidates with."""
        self.sync([entry()])
        tag = self.put_thumb(FILM, JPEG).json["etag"]
        got = self.fetch("/api/thumb/" + FILM,
                         headers={"If-None-Match": '"%s"' % tag})
        self.assertEqual(got.status, 304)

    def test_a_png_is_served_as_a_png(self):
        """The preparer makes JPEGs, but a sidecar in a collection can be a
        PNG, and serving one as image/jpeg would render as nothing."""
        self.sync([entry()])
        self.put_thumb(FILM, PNG, ctype="image/png")
        self.assertEqual(self.fetch("/api/thumb/" + FILM)
                         .header("Content-Type"), "image/png")

    def test_the_type_comes_from_the_bytes_and_not_the_header(self):
        """A client that mislabels a PNG as a JPEG still gets it served
        correctly, because what the daemon hands back is derived from what it
        actually holds."""
        self.sync([entry()])
        self.put_thumb(FILM, PNG, ctype="image/jpeg")
        self.assertEqual(self.fetch("/api/thumb/" + FILM)
                         .header("Content-Type"), "image/png")

    def test_something_that_is_not_an_image_is_refused(self):
        """Not a security control -- the bytes only ever come back with the
        type derived from them. It is so a mistyped curl leaves an error rather
        than a tile that renders as nothing on six phones."""
        self.sync([entry()])
        resp = self.put_thumb(FILM, b"<html>not a poster</html>")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.fetch("/api/thumb/" + FILM)
                         .header("Content-Type"), "image/svg+xml")

    def test_an_empty_body_is_refused(self):
        self.sync([entry()])
        self.assertEqual(self.put_thumb(FILM, b"").status, 400)

    def test_a_poster_for_an_unknown_film_is_a_404(self):
        """So the table cannot collect posters for films that will never show,
        and so a sync in the wrong order fails loudly rather than quietly."""
        self.assertEqual(self.put_thumb(FILM, JPEG).status, 404)

    def test_an_oversized_poster_is_refused_on_the_header_alone(self):
        """The cap exists so a body cannot be a way to make a 2 GB device
        allocate. Reading it to measure it would defeat that entirely.

        Driven over a raw socket rather than through fetch(), and the reason IS
        the property: the headers are sent, the body never is, and the daemon
        answers anyway. Through http.client the same request dies with a broken
        pipe partway through sending four megabytes -- which is the server
        behaving correctly and the client being unable to observe it.
        """
        self.sync([entry()])
        sock = self.raw(
            b"PUT /api/admin/thumb/" + FILM.encode() + b" HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(api.ADMIN_THUMB_MAX + 1).encode() +
            b"\r\n\r\n")
        answer = self.drain(sock)
        self.assertIn(b"413", answer.split(b"\r\n")[0])
        self.assertFalse(self.real_thumbs.have(FILM))

    def test_clearing_lets_the_extractor_try_again(self):
        """A sync deliberately leaves posters alone, so it cannot discard one
        somebody uploaded -- which means there has to be something else that
        says "that one was wrong"."""
        self.sync([entry()])
        self.put_thumb(FILM, JPEG)
        resp = self.fetch("/api/admin/thumb/%s/clear" % FILM, method="POST")
        self.assertEqual(resp.json, {"id": FILM, "cleared": True})
        self.assertEqual(self.fetch("/api/thumb/" + FILM)
                         .header("Content-Type"), "image/svg+xml")

    def test_clearing_an_unknown_film_is_a_404(self):
        self.assertEqual(
            self.fetch("/api/admin/thumb/%s/clear" % FILM,
                       method="POST").status, 404)

    def test_the_thumb_routes_match_the_whole_path(self):
        """Same rule as the audio route, and the reason both are regexes rather
        than a startswith-and-slice: "..", an uppercased id and a trailing
        segment all simply fail to match and fall through to the 404."""
        for bad in ["../../etc/passwd", FILM.upper(), FILM + "/..",
                    FILM + "x", ""]:
            with self.subTest(ident=bad):
                self.assertEqual(
                    self.fetch("/api/admin/thumb/" + bad, method="PUT",
                               body=JPEG).status, 404)


class EditsThroughTheApi(AdminTest):
    """/api/admin/item against the real store rather than the fake, so the
    reset-to-base behaviour is exercised where it actually lives."""

    def test_an_edit_shows_on_the_grid(self):
        self.sync([entry()])
        resp = self.fetch("/api/admin/item", method="POST",
                          body={"id": FILM, "fields": {"title": "Corrected"}})
        self.assertEqual(resp.json["title"], "Corrected")
        items = self.assertJson(self.fetch("/api/library"))["items"]
        self.assertEqual(items[0]["title"], "Corrected")

    def test_an_edit_survives_the_next_sync(self):
        """The property the whole database exists for. The file this replaced
        lived on a read-only mount and lost every edit at restart."""
        self.sync([entry()])
        self.fetch("/api/admin/item", method="POST",
                   body={"id": FILM, "fields": {"title": "Corrected"}})
        self.sync([entry()], reconcile=True)
        items = self.assertJson(self.fetch("/api/library"))["items"]
        self.assertEqual(items[0]["title"], "Corrected")

    def test_an_empty_title_goes_back_to_what_the_sync_said(self):
        self.sync([entry()])
        self.fetch("/api/admin/item", method="POST",
                   body={"id": FILM, "fields": {"title": "Wrong"}})
        resp = self.fetch("/api/admin/item", method="POST",
                          body={"id": FILM, "fields": {"title": ""}})
        self.assertEqual(resp.json["title"], "Ponyo")

    def test_a_path_bearing_field_is_still_never_editable(self):
        self.sync([entry()])
        self.fetch("/api/admin/item", method="POST", body={
            "id": FILM,
            "fields": {"media_rel": "../../etc/passwd", "path": "/etc/passwd"}})
        row = self.real_library.rows()[0]
        self.assertEqual(row["media_rel"], "Ponyo/Ponyo.mkv")
        self.assertEqual(row["overrides"], {})


if __name__ == "__main__":
    unittest.main()
