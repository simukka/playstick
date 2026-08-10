"""The control and metadata routes: everything the page polls or presses.

The audio route is big enough, and different enough, to live in its own file.
"""

import ipaddress
import json
import os
import unittest
from unittest import mock

from support import ApiTest, Busy, api, audio_track, patched, write_file


FILM = "0123456789abcdef"
OTHER = "fedcba9876543210"


class Health(ApiTest):
    def test_healthz_is_plain_and_open(self):
        resp = self.fetch("/healthz")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"ok\n")
        self.assertEqual(resp.header("Content-Type"), "text/plain")

    def test_healthz_answers_from_off_the_allowed_network(self):
        """Checked before _allowed() on purpose, and it has to stay that way:
        a health check that fails whenever the network filter is tightened is
        a health check that reports the wrong thing."""
        with patched(ALLOW_NETWORKS=_elsewhere()):
            self.assertEqual(self.fetch("/healthz").status, 200)


class AccessControl(ApiTest):
    def test_a_client_off_the_allowed_networks_is_refused(self):
        with patched(ALLOW_NETWORKS=_elsewhere()):
            for method, path in [("GET", "/"), ("GET", "/api/status"),
                                 ("GET", "/api/library"), ("POST", "/api/play"),
                                 ("POST", "/api/stop"), ("HEAD", "/api/status")]:
                with self.subTest(path=path, method=method):
                    resp = self.fetch(path, method=method)
                    self.assertEqual(resp.status, 403)

    def test_the_refusal_says_which_test_failed(self):
        with patched(ALLOW_NETWORKS=_elsewhere()):
            body = self.assertJson(self.fetch("/api/status"), 403)
        self.assertEqual(body, {"error": "not on the local network"})

    def test_an_empty_allow_list_lets_everybody_in(self):
        """The shipped default. ufw is purged by decision, so this filter is
        the only one there is, and 'unset' has to mean 'open' rather than
        'closed' -- a stick that answered nobody would look identical to one
        that had not booted."""
        self.assertEqual(self.fetch("/api/status").status, 200)

    def test_a_refused_client_never_reaches_the_player(self):
        self.library.add(FILM, "Ponyo")
        with patched(ALLOW_NETWORKS=_elsewhere()):
            self.fetch("/api/play", method="POST", body={"id": FILM})
            self.fetch("/api/stop", method="POST")
        self.assertEqual(self.player.calls, [])


class Routing(ApiTest):
    def test_the_ui_is_served_at_the_root(self):
        resp = self.fetch("/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.header("Content-Type"), "text/html; charset=utf-8")
        # The page is the whole application; a stale copy in a phone's cache
        # would be a bug nobody could see and nobody could clear.
        self.assertEqual(resp.header("Cache-Control"), "no-store")
        self.assertIn(b"<!DOCTYPE html>", resp.body[:64])

    def test_a_missing_ui_file_is_reported_rather_than_thrown(self):
        with patched(UI_FILE=os.path.join(api.__file__, "no-such-ui.html")):
            resp = self.fetch("/")
        self.assertEqual(resp.status, 500)
        self.assertEqual(resp.header("Content-Type"), "text/plain")
        self.assertIn(b"UI missing", resp.body)

    def test_unknown_routes_are_404_json(self):
        for method, path in [("GET", "/nope"), ("GET", "/api/nope"),
                             ("POST", "/api/nope"), ("POST", "/"),
                             ("GET", "/api"), ("GET", "/api/library/extra")]:
            with self.subTest(method=method, path=path):
                self.assertNotFound(self.fetch(path, method=method))

    def test_a_query_string_does_not_change_the_route(self):
        """The page uses ?debug for its sync overlay, so at least one request
        arrives with one. urlparse().path is what routes."""
        self.assertEqual(self.fetch("/?debug").status, 200)
        self.assertEqual(self.fetch("/api/status?t=123").status, 200)

    def test_head_is_only_for_the_audio_route(self):
        for path in ["/", "/healthz", "/api/status", "/api/library"]:
            with self.subTest(path=path):
                resp = self.fetch(path, method="HEAD")
                self.assertEqual(resp.status, 404)

    def test_a_head_response_carries_no_body(self):
        """Not a nicety. protocol_version is HTTP/1.1, so the connection stays
        open, and a body written after a HEAD's headers is read as the next
        response's status line -- the page would see garbage on a connection
        it had done nothing wrong with.

        Asked at the socket rather than through http.client, which discards
        its read buffer after a HEAD and would therefore swallow the evidence
        on most runs and not on others.
        """
        sock = self.raw(b"HEAD /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
        wire = self.drain(sock)
        self.assertIn(b"404", wire.split(b"\r\n", 1)[0])
        self.assertIn(b"Content-Length:", wire)
        self.assertTrue(wire.endswith(b"\r\n\r\n"),
                        "a body followed the headers: %r" % wire[-80:])
        self.assertNotIn(b"not found", wire.split(b"\r\n\r\n", 1)[1])

    def test_the_connection_is_reusable_after_a_head(self):
        conn = self.connect()
        head = self.fetch("/api/status", method="HEAD", conn=conn)
        self.assertEqual(head.status, 404)
        self.assertEqual(head.body, b"")
        after = self.fetch("/api/status", conn=conn)
        self.assertEqual(self.assertJson(after)["state"], "idle")


class Build(ApiTest):
    """Which build the page is, and how a phone finds out it is not that one.

    A deploy replaces ui.html and restarts the daemon. Nothing in that reaches
    a browser that already has the page: it polls /api/status and never
    navigates again. So the page is stamped on the way out, the stamp comes
    back on every poll, and a page whose stamp no longer matches reloads.

    The property everything below is really testing is that the stamp tracks
    the FILE. A build that changed on restart would order every phone in the
    house to reload after a power cut; one that did not change on a deploy
    would leave them on last week's JavaScript forever.
    """

    def setUp(self):
        super().setUp()
        # A copy, because these tests rewrite it. The shipped page is what
        # everything else in the suite serves.
        with open(api.UI_FILE, "rb") as fh:
            self.page = fh.read()
        self.stamp = 1700000000
        self.path = write_file("ui.html", self.page)
        os.utime(self.path, (self.stamp, self.stamp))
        self.ctx = patched(UI_FILE=self.path)
        self.ctx.start()
        self.addCleanup(self.ctx.stop)

    def rewrite(self, data):
        """A deploy, near enough: the file is replaced and its mtime moves.

        Moved by hand rather than left to the clock. A rewrite milliseconds
        after the last one is not what a deploy looks like, and a filesystem
        with coarse timestamps would decide these tests by luck.
        """
        write_file("ui.html", data)
        self.stamp += 60
        os.utime(self.path, (self.stamp, self.stamp))

    def served(self):
        return self.fetch("/").body

    def reported(self):
        return self.assertJson(self.fetch("/api/status"))["build"]

    def test_the_page_is_stamped_on_the_way_out(self):
        self.assertIn(b'var BUILD = "__PLAYSTICK_BUILD__";', self.page)
        self.assertNotIn(b"__PLAYSTICK_BUILD__", self.served())

    def test_the_stamp_is_what_the_status_route_reports(self):
        build = self.reported()
        self.assertTrue(build)
        self.assertIn(('var BUILD = "%s";' % build).encode(), self.served())

    def test_the_same_page_is_the_same_build(self):
        """Not merely stable within a request: the daemon is restarted by
        things that have nothing to do with the page -- a config change, a
        reboot, a re-provision that touched one Python module -- and a build
        that moved on any of those would reload every phone for nothing."""
        first = self.reported()
        for _ in range(3):
            self.assertEqual(self.reported(), first)
        self.assertEqual(self.fetch("/").body, self.served())

    def test_a_changed_page_is_a_changed_build(self):
        before = self.reported()
        self.rewrite(self.page + b"<!-- a deploy -->")
        self.assertNotEqual(self.reported(), before)

    def test_the_build_follows_the_bytes_and_not_the_timestamp(self):
        """A re-provision that changed nothing still rewrites this file, and
        `copy` gives it a new mtime every time. If that were the identity,
        every phone in the house would reload after every playbook run -- most
        of which have nothing to do with the page."""
        before = self.reported()
        self.rewrite(self.page)
        self.assertEqual(self.reported(), before)

    def test_a_changed_page_is_served_changed(self):
        """The cache is keyed on a stat, and a cache that answered from the
        wrong generation would report a new build alongside the old bytes --
        every phone reloading into exactly the page it already had."""
        self.rewrite(self.page.replace(b"<title>Playstick</title>",
                                       b"<title>Second</title>"))
        self.assertIn(b"<title>Second</title>", self.served())
        self.assertIn(('var BUILD = "%s";' % self.reported()).encode(),
                      self.served())

    def test_a_page_with_no_stamp_still_has_a_build(self):
        """Nothing enforces that the placeholder is in the file, and an older
        ui.html predates it entirely. Serving that unchanged is right; refusing
        to serve it, or serving it with no build at all, is not."""
        self.rewrite(b"<!DOCTYPE html><title>bare</title>")
        self.assertTrue(self.reported())
        self.assertEqual(self.served(), b"<!DOCTYPE html><title>bare</title>")

    def test_a_missing_page_does_not_take_the_status_route_with_it(self):
        """/api/status is what the phones poll and what a health check reads.
        A UI file that vanished between two polls is a broken deploy, and a 500
        on every poll would hide the rest of what the daemon still knows."""
        good = self.reported()
        os.remove(self.path)
        self.assertEqual(self.fetch("/").status, 500)
        self.assertEqual(self.reported(), good)


class Library(ApiTest):
    def test_an_empty_library_still_answers(self):
        self.library.available = False
        self.library.error = "the share is not mounted"
        body = self.assertJson(self.fetch("/api/library"))
        self.assertEqual(body["items"], [])
        self.assertFalse(body["available"])
        self.assertEqual(body["error"], "the share is not mounted")
        self.assertEqual(body["scanned_at"], 1700000000.0)

    def test_items_keep_the_library_order(self):
        for ident, title in [(FILM, "Ponyo"), (OTHER, "Arrietty")]:
            self.library.add(ident, title)
        body = self.assertJson(self.fetch("/api/library"))
        self.assertEqual([i["id"] for i in body["items"]], [FILM, OTHER])
        self.assertEqual([i["title"] for i in body["items"]], ["Ponyo", "Arrietty"])

    def test_index_metadata_is_passed_through_when_it_is_there(self):
        self.library.add(FILM, "Ponyo", year=2008, rating="U",
                         genres=["Animation", "Family"])
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertEqual(item["year"], 2008)
        self.assertEqual(item["rating"], "U")
        self.assertEqual(item["genres"], ["Animation", "Family"])

    def test_a_film_found_by_walking_the_share_has_null_metadata(self):
        """No index, so no year and no rating -- and the tile has to render
        anyway. The keys are present and empty rather than absent, so the page
        never has to distinguish 'not in the payload' from 'not known'."""
        self.library.add(FILM, "Ponyo")
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertIsNone(item["year"])
        self.assertIsNone(item["rating"])
        self.assertEqual(item["genres"], [])
        self.assertEqual(item["sort_title"], "")

    def test_the_shelf_key_is_passed_through_for_the_name_sort(self):
        """prep files "The Fifth Element" under F, and the page's A-to-Z has to
        agree with the order the index already arrives in."""
        self.library.add(FILM, "The Fifth Element", sort_title="fifth element")
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertEqual(item["sort_title"], "fifth element")

    def test_hidden_is_false_by_default_and_carried_when_set(self):
        """Every client is told whether a film is hidden -- the phones so they
        can leave it out, the curator so it can be un-hidden. Present and false
        for a film nobody has touched, so the page never has to tell 'not in
        the payload' from 'not hidden'."""
        self.library.add(FILM, "Ponyo")
        self.library.add(OTHER, "Grave of the Fireflies", hidden=True)
        items = self.assertJson(self.fetch("/api/library"))["items"]
        by_id = {i["id"]: i for i in items}
        self.assertFalse(by_id[FILM]["hidden"])
        self.assertTrue(by_id[OTHER]["hidden"])

    def test_has_thumb_is_true_for_a_poster_from_the_index(self):
        self.library.add(FILM, "Ponyo", poster="/srv/movies/Ponyo/poster.jpg")
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertTrue(item["has_thumb"])

    def test_has_thumb_is_true_for_a_frame_already_extracted(self):
        self.library.add(FILM, "Ponyo")
        self.thumbs.have_ids.add(FILM)
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertTrue(item["has_thumb"])

    def test_has_thumb_is_false_when_there_is_nothing_yet(self):
        self.library.add(FILM, "Ponyo")
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertFalse(item["has_thumb"])

    def test_audio_langs_are_deduplicated_and_sorted(self):
        """The grid's sheet offers a PREFERRED language before any film has
        started, so what it needs is the set of languages the library has --
        not which numbered track any one film keeps them on."""
        self.library.add(FILM, "Ponyo", audio=[
            audio_track(0, "jpn", "Japanese"),
            audio_track(1, "eng", "English"),
            audio_track(2, "eng", "English commentary"),
        ])
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertEqual(item["audio_langs"], ["eng", "jpn"])

    def test_a_film_nobody_prepped_offers_no_languages(self):
        self.library.add(FILM, "Ponyo")
        item = self.assertJson(self.fetch("/api/library"))["items"][0]
        self.assertEqual(item["audio_langs"], [])

    def test_the_payload_is_not_cached(self):
        self.assertEqual(self.fetch("/api/library").header("Cache-Control"),
                         "no-store")


class Admin(ApiTest):
    """The desktop curator route. Shaped like /api/play -- an id that has to
    name a real film, a body coerced at the boundary, a status JSON back -- and
    as careful: nothing it accepts can reach the filesystem."""

    def edit(self, ident, **fields):
        return self.fetch("/api/admin/item", method="POST",
                          body={"id": ident, "fields": fields})

    def test_an_edit_lands_on_the_film_and_comes_back(self):
        self.library.add(FILM, "ponyo.2008.1080p")
        body = self.assertJson(self.edit(FILM, title="Ponyo", year=2008,
                                         genres=["Animation", "Family"]))
        self.assertEqual(body["title"], "Ponyo")
        self.assertEqual(body["year"], 2008)
        self.assertEqual(body["genres"], ["Animation", "Family"])
        self.assertEqual(self.library.items[FILM]["title"], "Ponyo")

    def test_hiding_a_film_is_an_edit_like_any_other(self):
        self.library.add(FILM, "Grave of the Fireflies")
        body = self.assertJson(self.edit(FILM, hidden=True))
        self.assertTrue(body["hidden"])
        self.assertTrue(self.library.items[FILM]["hidden"])
        # ...and un-hiding it again.
        body = self.assertJson(self.edit(FILM, hidden=False))
        self.assertFalse(body["hidden"])

    def test_an_unknown_film_is_a_404(self):
        body = self.assertJson(self.edit("deadbeefdeadbeef", title="Nope"), 404)
        self.assertIn("not in the library", body["error"])

    def test_a_year_that_is_not_a_number_resets_rather_than_throws(self):
        """An untrusted value arrives here, so it is coerced where /api/volume
        coerces its own -- a year of "loud" is a cleared field, not a 500."""
        self.library.add(FILM, "Ponyo", year=2008)
        self.assertJson(self.edit(FILM, year="loud"))
        self.assertEqual(self.library.overrides[-1][1]["year"], None)

    def test_an_emptied_title_is_a_reset_not_an_empty_title(self):
        """A blank box means 'go back to what the index said', which the daemon
        expresses as None on the field rather than as the literal empty string
        a naive save would send."""
        self.library.add(FILM, "Ponyo")
        self.assertJson(self.edit(FILM, title="   "))
        self.assertEqual(self.library.overrides[-1][1]["title"], None)

    def test_a_path_bearing_field_is_never_editable(self):
        """The one invariant the package rests on: no path the client sent
        reaches the filesystem. The editor may set metadata and nothing else,
        so an attempt to substitute a poster or a media path is dropped before
        it is ever passed to the library."""
        self.library.add(FILM, "Ponyo")
        self.fetch("/api/admin/item", method="POST", body={
            "id": FILM,
            "fields": {"title": "Ponyo", "path": "/etc/passwd",
                       "poster": "/etc/shadow"},
        })
        sent = self.library.overrides[-1][1]
        self.assertNotIn("path", sent)
        self.assertNotIn("poster", sent)
        self.assertEqual(sent["title"], "Ponyo")

    def test_a_body_that_is_not_an_object_does_not_throw(self):
        self.library.add(FILM, "Ponyo")
        # No fields at all: nothing to change, and certainly not a crash.
        self.assertJson(self.edit(FILM))


class State(ApiTest):
    """The one field both /api/status and /api/library carry, and the only
    thing that tells the page whether to draw a grid, a player or an apology."""

    def _state(self, path="/api/status"):
        return self.assertJson(self.fetch(path))["state"]

    def test_idle_when_the_share_is_there_and_nothing_is_playing(self):
        self.assertEqual(self._state(), "idle")
        self.assertEqual(self._state("/api/library"), "idle")

    def test_unavailable_when_the_share_is_not_there(self):
        self.library.available = False
        self.assertEqual(self._state(), "unavailable")

    def test_airplay_outranks_an_unmounted_share(self):
        """Somebody is mirroring. That is the more useful thing to say, and it
        is also the state that explains why pressing play will be refused."""
        self.library.available = False
        with patched(airplay_active=lambda: True):
            self.assertEqual(self._state(), "airplay")

    def test_playing_outranks_everything(self):
        """A film that is on screen cannot be described as anything else, and
        the check is short-circuited before airplay_active() -- which matters
        beyond tidiness, because that one shells out to ss on the device and
        the page asks this question once a second per phone."""
        self.library.available = False
        self.player.playing = "playing"
        with patched(airplay_active=_explode):
            self.assertEqual(self._state(), "playing")

    def test_paused_is_reported_as_itself(self):
        self.player.playing = "paused"
        self.assertEqual(self._state(), "paused")


class Status(ApiTest):
    def test_idle_status_has_the_shape_the_page_expects(self):
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["state"], "idle")
        self.assertEqual(body["id"], "")
        self.assertEqual(body["title"], "")
        self.assertEqual(body["position"], 0)
        self.assertFalse(body["position_valid"])
        self.assertFalse(body["buffering"])
        self.assertEqual(body["tracks"], [])
        self.assertEqual(body["thumbs_pending"], 0)
        self.assertIsNone(body["timecode"])

    def test_playing_status_names_the_film(self):
        item = self.library.add(FILM, "Ponyo")
        self.player.item = item
        self.player.playing = "playing"
        self.player.data = {"position": 61.5, "duration": 5400.0, "volume": 70}
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["id"], FILM)
        self.assertEqual(body["title"], "Ponyo")
        self.assertEqual(body["position"], 61.5)
        self.assertTrue(body["position_valid"])
        self.assertEqual(body["duration"], 5400.0)
        self.assertEqual(body["volume"], 70)

    def test_a_position_mpv_has_not_reported_is_not_a_position_of_zero(self):
        """The distinction the progress bar does not need and a phone syncing
        its headphones to the film does: position stays 0 for the bar, and
        position_valid says not to believe it."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": None, "duration": 5400.0}
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["position"], 0)
        self.assertFalse(body["position_valid"])

    def test_position_zero_is_valid(self):
        """A film in its first second. Distinguishing this from the case above
        is the entire reason position_valid exists."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": 0.0}
        body = self.assertJson(self.fetch("/api/status"))
        self.assertTrue(body["position_valid"])

    def test_the_timecode_is_passed_through_whole(self):
        """Four keys that only mean anything together: where the film was, the
        instant on this machine's clock when it was there, whether it is
        moving, and which timeline that belongs to. A phone evaluates the line
        they describe against its own clock -- see Player._advance()."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        tc = self.player.timecode(1421.834, 918273.4551, rate=1.0, epoch=7)
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["timecode"], tc)

    def test_the_timecode_is_on_the_clock_the_time_route_publishes(self):
        """`at` is meaningless except against /api/time. If the two ever came
        off different clocks, every listener would be wrong by the difference
        and nothing in either payload would show it."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        now = self.assertJson(self.fetch("/api/time"))["now"]
        self.player.timecode(10.0, now)
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["timecode"]["at"], now)

    def test_a_film_mpv_has_not_opened_yet_has_no_timecode(self):
        """Null rather than a timecode at zero, for the same reason
        position_valid exists: a phone that believed the second one would
        place its audio at the start of the film."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": None, "duration": 5400.0}
        self.assertIsNone(self.assertJson(self.fetch("/api/status"))["timecode"])

    def test_buffering_is_reported_as_a_boolean(self):
        """The page pauses every listener's headphones on this, so it must be
        a bool and not mpv's paused-for-cache string."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": 12.0, "buffering": "yes"}
        self.assertIs(self.assertJson(self.fetch("/api/status"))["buffering"], True)

    def test_the_tracks_a_phone_can_ask_for(self):
        item = self.library.add(FILM, "Ponyo", audio=[
            audio_track(0, "jpn", "Japanese", channels=6, default=True),
            audio_track(1, "eng", "English", channels=2, default=False,
                        offset=1.5),
        ])
        self.player.item = item
        self.player.playing = "playing"
        tracks = self.assertJson(self.fetch("/api/status"))["tracks"]
        self.assertEqual([t["n"] for t in tracks], [0, 1])
        self.assertEqual([t["lang"] for t in tracks], ["jpn", "eng"])
        self.assertEqual(tracks[0]["channels"], 6)
        self.assertTrue(tracks[0]["default"])
        # Measured by prep, not assumed: the page adds it to its sync target,
        # so a container prep could not normalise is still fixable here.
        self.assertEqual(tracks[1]["offset"], 1.5)

    def test_a_track_never_carries_its_path(self):
        """No filesystem path crosses this boundary in either direction. The
        phone asks for a track by number and never learns where it lives."""
        self.player.item = self.library.add(
            FILM, "Ponyo", audio=[audio_track(path="/srv/movies/Ponyo/eng.m4a")])
        self.player.playing = "playing"
        body = self.fetch("/api/status").body
        self.assertNotIn(b"/srv/movies", body)
        self.assertNotIn(b"path", body)

    def test_a_film_nobody_prepped_has_no_tracks(self):
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.assertEqual(self.assertJson(self.fetch("/api/status"))["tracks"], [])

    def test_the_two_audio_flags_are_independent(self):
        """`audio` is whether the PROJECTOR has sound -- false on this
        appliance -- and `phone_audio` is whether this build serves headphone
        audio at all. The page hides different controls on each."""
        self.player.item = self.library.add(FILM, "Ponyo",
                                            audio=[audio_track()])
        self.player.playing = "playing"
        with patched(HAS_AUDIO=False, PHONE_AUDIO=True):
            body = self.assertJson(self.fetch("/api/status"))
        self.assertFalse(body["audio"])
        self.assertTrue(body["phone_audio"])
        self.assertEqual(len(body["tracks"]), 1)

    def test_a_build_without_phone_audio_offers_no_tracks(self):
        self.player.item = self.library.add(FILM, "Ponyo",
                                            audio=[audio_track()])
        self.player.playing = "playing"
        with patched(PHONE_AUDIO=False):
            body = self.assertJson(self.fetch("/api/status"))
        self.assertFalse(body["phone_audio"])
        self.assertEqual(body["tracks"], [])

    def test_pending_thumbnails_are_counted(self):
        self.thumbs.pending_count = 3
        self.assertEqual(self.assertJson(self.fetch("/api/status"))["thumbs_pending"], 3)

    def test_status_is_not_cached(self):
        self.assertEqual(self.fetch("/api/status").header("Cache-Control"),
                         "no-store")


class SyncTelemetry(ApiTest):
    """A listening phone's own numbers, carried on the status poll and written
    to the journal. The page sends the header only when ?debug is in its URL,
    so that gate is on the client; what is tested here is that the daemon
    treats the value as what it is -- untrusted input from an unauthenticated
    network, on its way into a log file."""

    BLOB = "v=1;id=8f2c;t=612.4;st=play;err=-38;ahead=48.2;w=1;lag=22;ls=0"

    def setUp(self):
        super().setUp()
        self.lines = []
        patcher = mock.patch.object(
            api, "log", lambda msg, *args: self.lines.append(msg % args if args else msg))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The rate limiter is module state and outlives a test. Start each one
        # with a fresh budget so an earlier test cannot silence a later one.
        api._sync_window[0] = 0.0
        api._sync_window[1] = 0

    def poll(self, blob=BLOB, headers=None):
        headers = dict(headers or {})
        if blob is not None:
            headers["X-Playstick-Sync"] = blob
        return self.fetch("/api/status", headers=headers)

    def test_a_poll_without_the_header_logs_nothing(self):
        """Which is every poll from every phone that has not got ?debug open.
        The page polls once a second each, so the quiet path has to stay
        quiet."""
        self.assertEqual(self.fetch("/api/status").status, 200)
        self.assertEqual(self.lines, [])

    def test_the_blob_is_logged_verbatim(self):
        self.assertEqual(self.poll().status, 200)
        self.assertEqual(len(self.lines), 1)
        self.assertIn(self.BLOB, self.lines[0])

    def test_the_line_carries_what_the_daemon_believed_at_the_same_moment(self):
        """Half the candidate faults are disagreements between mpv's view and
        the element's, so a line that recorded only the phone's half could not
        show one."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": 1421.826, "buffering": True}
        self.poll()
        line = self.lines[0]
        self.assertIn("playing", line)
        self.assertIn("pos=1421.83", line)
        self.assertIn("buf=1", line)
        self.assertIn("127.0.0.1", line)

    def test_a_position_mpv_has_not_given_yet_is_not_logged_as_zero(self):
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": None}
        self.poll()
        self.assertIn("pos=?", self.lines[0])

    def test_the_response_is_unchanged_by_the_header(self):
        """Telemetry must not become a second code path through the route the
        whole page depends on."""
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"
        self.player.data = {"position": 12.0}
        plain = self.assertJson(self.fetch("/api/status"))
        with_header = self.assertJson(self.poll())
        self.assertEqual(plain, with_header)

    def test_a_newline_cannot_forge_a_second_journal_entry(self):
        """The one that matters, and it has to be asked at the socket: an
        attacker would not be using http.client, which refuses to send this at
        all. journald is line-oriented, so a newline that reached the log call
        would be a fabricated entry of the sender's choosing."""
        self.drain(self.raw(
            b"GET /api/status HTTP/1.1\r\nHost: x\r\n"
            b"X-Playstick-Sync: v=1;st=play\n"
            b"Aug 01 00:00:00 stick sudo: root : COMMAND=/bin/sh\r\n\r\n"))
        for line in self.lines:
            self.assertNotIn("\n", line)
            self.assertNotIn("sudo", line)

    def test_a_carriage_return_cannot_either(self):
        self.drain(self.raw(
            b"GET /api/status HTTP/1.1\r\nHost: x\r\n"
            b"X-Playstick-Sync: v=1;st=play\r\nX-Forged: yes\r\n\r\n"))
        for line in self.lines:
            self.assertNotIn("\r", line)
            self.assertNotIn("Forged", line)

    def test_a_percent_cannot_reach_a_format_string(self):
        """Belt and braces: the filter drops '%' and the log call passes the
        value as an argument rather than interpolating it, so neither alone has
        to be right."""
        self.poll("v=1;st=%s%d%%;err=1")
        self.assertEqual(len(self.lines), 1)
        self.assertNotIn("%", self.lines[0])
        self.assertTrue(self.lines[0].endswith("v=1;st=sd;err=1"))

    def test_only_a_known_alphabet_survives(self):
        self.poll("v=1;a=<script>;b=`id`;c=$(id);d='x';e=\"y\";f=a|b&c")
        line = self.lines[0]
        for bad in "<>`$()'\"|&":
            self.assertNotIn(bad, line)
        # ...and the shape of the record is still there to read.
        self.assertIn("v=1;a=script;b=id;c=id;d=x;e=y;f=abc", line)

    def test_an_oversized_value_is_truncated_rather_than_refused(self):
        """A phone sending too much is a bug in the page, not an attack, and
        the first SYNC_MAX characters of it are still the diagnostic."""
        self.poll("v=1;pad=" + "9" * 5000)
        self.assertEqual(len(self.lines), 1)
        self.assertLessEqual(len(self.lines[0].split(" ", 5)[-1]), api.SYNC_MAX)

    def test_the_cap_leaves_room_for_what_the_page_actually_sends(self):
        """The page's blob is ~240 characters plus however much of `tun` a
        listener has generated by adjusting constants in the debug sheet. A cap
        that clipped a real line would silently drop the last fields, which are
        the stall counters."""
        self.assertGreaterEqual(api.SYNC_MAX, 480)

    def test_a_header_with_nothing_usable_in_it_is_dropped(self):
        self.poll("<>|&")
        self.assertEqual(self.lines, [])

    def test_an_empty_header_is_dropped(self):
        self.poll("")
        self.assertEqual(self.lines, [])

    def test_the_journal_cannot_be_flooded(self):
        """Six phones at one line a second is the design load. A client stuck
        in a retry loop shares 32 GB of eMMC with everything else on the
        device, so the budget is global and per second."""
        for _ in range(api.SYNC_MAX_RATE + 25):
            self.assertEqual(self.poll().status, 200)
        self.assertEqual(len(self.lines), api.SYNC_MAX_RATE)

    def test_the_budget_refills(self):
        for _ in range(api.SYNC_MAX_RATE + 5):
            self.poll()
        self.assertEqual(len(self.lines), api.SYNC_MAX_RATE)
        api._sync_window[0] = 0.0            # as a fresh second would
        self.poll()
        self.assertEqual(len(self.lines), api.SYNC_MAX_RATE + 1)

    def test_a_refused_line_does_not_refuse_the_poll(self):
        """Over budget means the telemetry is dropped. It must never mean the
        page stops being told what is playing."""
        for _ in range(api.SYNC_MAX_RATE + 1):
            resp = self.poll()
        self.assertEqual(self.assertJson(resp)["state"], "idle")

    def test_telemetry_rides_only_on_the_status_poll(self):
        """The route the page already calls once a second. Nothing else needs
        to grow a logging side effect to carry it."""
        for path in ["/api/library", "/healthz", "/"]:
            with self.subTest(path=path):
                self.fetch(path, headers={"X-Playstick-Sync": self.BLOB})
        self.assertEqual(self.lines, [])

    def test_a_client_off_the_network_cannot_write_to_the_journal(self):
        with patched(ALLOW_NETWORKS=_elsewhere()):
            self.assertEqual(self.poll().status, 403)
        self.assertEqual(self.lines, [])


class Play(ApiTest):
    def test_playing_a_film_starts_it_and_returns_the_new_status(self):
        self.library.add(FILM, "Ponyo")
        body = self.assertJson(self.fetch("/api/play", method="POST",
                                          body={"id": FILM}))
        self.assertEqual(self.player.calls, [("start", FILM)])
        # The response IS a status, so the page needs no second round trip
        # before it can redraw.
        self.assertEqual(body["state"], "playing")
        self.assertEqual(body["title"], "Ponyo")

    def test_an_unknown_id_is_refused_in_words_a_child_can_read(self):
        body = self.assertJson(self.fetch("/api/play", method="POST",
                                          body={"id": OTHER}), 404)
        self.assertEqual(body, {"error": "That film is not in the library."})
        self.assertEqual(self.player.calls, [])

    def test_a_request_with_no_id_is_a_missing_film(self):
        for payload in [{}, {"id": ""}, {"id": None}, {"other": FILM}]:
            with self.subTest(payload=payload):
                resp = self.fetch("/api/play", method="POST", body=payload)
                self.assertEqual(resp.status, 404)
        self.assertEqual(self.player.calls, [])

    def test_a_body_that_is_not_an_object_is_a_missing_film_not_a_crash(self):
        """Including the ones that parse. A bare list reaches .get() if it is
        not screened off, and an AttributeError in a request thread is a
        connection the page never gets an answer on."""
        for raw in [b"", b"not json", b'{"id":', b"[1,2,3]", b"42", b'"' + FILM.encode() + b'"']:
            with self.subTest(raw=raw):
                conn = self.connect()
                conn.request("POST", "/api/play", body=raw,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 404)
                conn.close()
        self.assertEqual(self.player.calls, [])

    def test_an_id_that_is_not_a_string_cannot_reach_the_library(self):
        """str() before the lookup, so a list or a dict is a key miss rather
        than a TypeError out of a dict lookup."""
        for value in [123, ["a"], {"a": 1}, True]:
            with self.subTest(value=value):
                resp = self.fetch("/api/play", method="POST", body={"id": value})
                self.assertEqual(resp.status, 404)

    def test_a_busy_player_answers_409_and_says_why(self):
        """Somebody is mirroring over AirPlay. Not an error the page should
        report as a failure -- it is a state, and the answer carries it."""
        self.library.add(FILM, "Ponyo")
        self.player.start_error = Busy("Somebody is mirroring to the projector.")
        with patched(airplay_active=lambda: True):
            body = self.assertJson(self.fetch("/api/play", method="POST",
                                              body={"id": FILM}), 409)
        self.assertEqual(body["error"], "Somebody is mirroring to the projector.")
        self.assertEqual(body["state"], "airplay")

    def test_a_player_that_will_not_start_is_a_500_without_a_traceback(self):
        self.library.add(FILM, "Ponyo")
        self.player.start_error = OSError("no such file: /usr/bin/mpv")
        body = self.assertJson(self.fetch("/api/play", method="POST",
                                          body={"id": FILM}), 500)
        self.assertEqual(body, {"error": "The player would not start."})
        # Whatever went wrong belongs in the journal, not on a child's phone.
        self.assertNotIn("mpv", json.dumps(body))

    def test_the_whole_library_entry_reaches_the_player(self):
        """Not just the id: Player.start needs the path and the subtitle and
        audio sidecars the index recorded."""
        item = self.library.add(FILM, "Ponyo", path="/srv/movies/Ponyo.mkv")
        seen = []
        self.player.start = lambda i: seen.append(i)
        self.fetch("/api/play", method="POST", body={"id": FILM})
        self.assertEqual(seen, [item])


class Controls(ApiTest):
    def setUp(self):
        super().setUp()
        self.player.item = self.library.add(FILM, "Ponyo")
        self.player.playing = "playing"

    def test_pause_and_resume(self):
        body = self.assertJson(self.fetch("/api/pause", method="POST"))
        self.assertEqual(self.player.calls, [("set_pause", True)])
        self.assertEqual(body["state"], "paused")

        body = self.assertJson(self.fetch("/api/resume", method="POST"))
        self.assertEqual(self.player.calls[-1], ("set_pause", False))
        self.assertEqual(body["state"], "playing")

    def test_stop(self):
        body = self.assertJson(self.fetch("/api/stop", method="POST"))
        self.assertEqual(self.player.calls, [("stop",)])
        self.assertEqual(body["state"], "idle")
        self.assertEqual(body["id"], "")

    def test_volume_passes_the_delta_through(self):
        self.fetch("/api/volume", method="POST", body={"delta": 5})
        self.fetch("/api/volume", method="POST", body={"delta": -5})
        self.assertEqual(self.player.calls,
                         [("nudge_volume", 5), ("nudge_volume", -5)])

    def test_volume_without_a_delta_is_a_nudge_of_nothing(self):
        self.fetch("/api/volume", method="POST")
        self.assertEqual(self.player.calls, [("nudge_volume", 0)])

    def test_a_delta_that_is_not_a_number_is_a_nudge_of_nothing(self):
        """The player does arithmetic on this. Screened at the boundary the
        untrusted value crosses, so the request is answered rather than the
        connection dropped."""
        for value in ["loud", None, [1], {"a": 1}]:
            with self.subTest(value=value):
                resp = self.fetch("/api/volume", method="POST",
                                  body={"delta": value})
                self.assertEqual(resp.status, 200)
        self.assertEqual(self.player.calls, [("nudge_volume", 0)] * 4)

    def test_a_numeric_delta_reaches_the_player_as_an_integer(self):
        self.fetch("/api/volume", method="POST", body={"delta": "5"})
        self.assertEqual(self.player.calls, [("nudge_volume", 5)])

    def test_rescan_wakes_the_scanner_and_does_not_wait_for_it(self):
        """A scan over CIFS takes seconds. The answer is an acknowledgement,
        not a library -- the page polls for the result."""
        body = self.assertJson(self.fetch("/api/rescan", method="POST"))
        self.assertEqual(body, {"ok": True})
        self.assertEqual(self.library.rescans, 1)

    def test_the_controls_refuse_GET(self):
        for path in ["/api/play", "/api/pause", "/api/stop", "/api/volume",
                     "/api/rescan"]:
            with self.subTest(path=path):
                self.assertNotFound(self.fetch(path))


class Thumbs(ApiTest):
    def test_an_unknown_film_has_no_poster(self):
        self.assertNotFound(self.fetch("/api/thumb/" + OTHER))

    def test_a_poster_from_the_index_is_served_and_briefly_cached(self):
        """A day, not a year: re-running prep can replace this file, and a
        phone that cached it for a year would never find out."""
        poster = write_file("posters/ponyo.jpg", b"\xff\xd8\xff\xe0 poster")
        self.library.add(FILM, "Ponyo", poster=poster)
        resp = self.fetch("/api/thumb/" + FILM)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.header("Content-Type"), "image/jpeg")
        self.assertEqual(resp.header("Cache-Control"), "public, max-age=86400")
        self.assertEqual(resp.body, b"\xff\xd8\xff\xe0 poster")

    def test_an_extracted_frame_is_immutable(self):
        """Keyed by a hash of the film, so this URL can only ever mean this
        image -- which is what lets the grid re-render without refetching."""
        self.library.add(FILM, "Ponyo")
        with open(api.Thumbs.cached_path(FILM), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0 frame")
        self.addCleanup(os.unlink, api.Thumbs.cached_path(FILM))
        resp = self.fetch("/api/thumb/" + FILM)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.header("Content-Type"), "image/jpeg")
        self.assertEqual(resp.header("Cache-Control"),
                         "public, max-age=31536000, immutable")
        self.assertEqual(resp.body, b"\xff\xd8\xff\xe0 frame")

    def test_a_film_with_no_poster_gets_a_placeholder_and_queues_the_work(self):
        """The grid never waits. Pulling a frame out of a film over CIFS costs
        seconds, so the tile is answered immediately and the extraction is
        queued behind it."""
        self.library.add(FILM, "Ponyo")
        resp = self.fetch("/api/thumb/" + FILM)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.header("Content-Type"), "image/svg+xml")
        # no-store, so the request after the frame lands gets the real thing
        # without the page having to cache-bust.
        self.assertEqual(resp.header("Cache-Control"), "no-store")
        self.assertIn(b"<svg", resp.body)
        self.assertIn(b">P<", resp.body)
        self.assertEqual(self.thumbs.requested, [FILM])

    def test_the_build_stamp_is_a_cache_key_and_nothing_more(self):
        """The page appends ?v=<build> so that a deploy re-pulls posters a
        phone would otherwise hold for a year. The route matches against the
        parsed path, so nothing here reads it -- which is the property that
        makes putting it in the query safe. The id is still the only thing the
        client sends that this route acts on.
        """
        poster = write_file("posters/ponyo.jpg", b"\xff\xd8\xff\xe0 poster")
        self.library.add(FILM, "Ponyo", poster=poster)
        plain = self.fetch("/api/thumb/" + FILM)
        for query in ["?v=c20e48476c19", "?v=c20e48476c19&t=1700000000",
                      "?v=", "?v=../../etc/passwd"]:
            with self.subTest(query=query):
                resp = self.fetch("/api/thumb/" + FILM + query)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.body, plain.body)
                self.assertEqual(resp.header("Cache-Control"),
                                 plain.header("Cache-Control"))

    def test_a_stamped_url_for_an_unknown_film_is_still_a_404(self):
        """The query is not a way in. Whatever is after the '?', the id in the
        path is what has to name a film."""
        self.assertNotFound(self.fetch("/api/thumb/" + OTHER + "?v=abc"))

    def test_a_poster_the_share_lost_falls_back_rather_than_failing(self):
        """The index said there was one and the NAS says otherwise. That is a
        placeholder and a queued extraction, not a 500."""
        self.library.add(FILM, "Ponyo", poster="/srv/movies/gone/poster.jpg")
        resp = self.fetch("/api/thumb/" + FILM)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.header("Content-Type"), "image/svg+xml")
        self.assertEqual(self.thumbs.requested, [FILM])

    def test_a_title_cannot_break_out_of_the_placeholder(self):
        """The only place a library title is interpolated into markup. It is
        one character of it, and that character is escaped."""
        self.library.add(FILM, '<script>alert(1)</script>')
        resp = self.fetch("/api/thumb/" + FILM)
        self.assertNotIn(b"<script", resp.body)
        self.assertIn(b"&lt;", resp.body)

    def test_the_thumb_route_is_a_lookup_and_not_a_path(self):
        """Unlike the audio route this one slices the URL rather than matching
        it, so the traversal defence is that the result is a dict key and
        never touches the filesystem."""
        for suffix in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "",
                       FILM + "/../" + OTHER, "%00" + FILM]:
            with self.subTest(suffix=suffix):
                self.assertNotFound(self.fetch("/api/thumb/" + suffix))


class Preparing(ApiTest):
    """The state between a tap on a poster and a film on the screen.

    All of it is additive: `prepare`, `notice` and `projector` are new keys on
    a payload that already had a dozen, and a ui.html that has never heard of
    them ignores them exactly as it ignores everything else it does not know.
    """

    def preparing(self, step="warming", label="Waiting for the lamp…", since=3.0):
        self.projectionist.prepare = {"step": step, "label": label,
                                      "since": since}

    def test_play_goes_through_the_projectionist_not_the_player(self):
        """The POST now answers as soon as the film is accepted. Everything
        after that -- the lamp, the input, the display, mpv -- happens on a
        thread, because a cold lamp takes longer to light than a browser will
        hold a request open."""
        self.library.add(FILM, "Ponyo")
        self.fetch("/api/play", method="POST", body={"id": FILM})
        self.assertEqual(self.projectionist.calls, [("begin", FILM)])

    def test_the_state_is_preparing_while_it_prepares(self):
        self.preparing()
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["state"], "preparing")

    def test_the_step_and_its_words_reach_the_page(self):
        """The label is the server's, verbatim. Two copies of the wording --
        one here and one in the page -- drift, and the one on the phone is the
        copy nobody notices is wrong."""
        self.preparing(step="warming", label="Waiting for the lamp…")
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["prepare"]["step"], "warming")
        self.assertEqual(body["prepare"]["label"], "Waiting for the lamp…")

    def test_prepare_is_null_when_nothing_is_being_prepared(self):
        self.assertIsNone(self.assertJson(self.fetch("/api/status"))["prepare"])

    def test_a_film_names_itself_before_mpv_exists(self):
        """The preparing view draws the poster, and the id is how it asks for
        one. There is no mpv yet to be asked."""
        item = self.library.add(FILM, "Ponyo")
        self.player.item = item
        self.preparing()
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["id"], FILM)
        self.assertEqual(body["title"], "Ponyo")

    def test_the_library_route_reports_preparing_too(self):
        """It carries the same state field, and a grid that thought the
        projector was free would offer a second film."""
        self.library.add(FILM, "Ponyo")
        self.preparing()
        self.assertEqual(self.assertJson(self.fetch("/api/library"))["state"],
                         "preparing")

    def test_a_second_film_while_one_is_being_prepared_is_refused(self):
        self.library.add(FILM, "Ponyo")
        self.projectionist.begin_error = Busy("A movie is already starting.")
        body = self.assertJson(self.fetch("/api/play", method="POST",
                                          body={"id": FILM}), 409)
        self.assertEqual(body["error"], "A movie is already starting.")

    def test_stop_abandons_a_preparation(self):
        """One button on the page, two things it may mean. The daemon works
        out which, so the page does not have to know what state it is in."""
        self.preparing()
        body = self.assertJson(self.fetch("/api/stop", method="POST"))
        self.assertIn(("stop",), self.projectionist.calls)
        self.assertEqual(body["state"], "idle")

    def test_the_projector_is_described_on_every_poll(self):
        self.projectionist.projector = {"model": "pt-ae4000", "power": "on",
                                        "fault": ""}
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["projector"],
                         {"model": "pt-ae4000", "power": "on", "fault": ""})

    def test_a_fault_reaches_the_page_in_one_sentence(self):
        self.projectionist.projector = {"model": "pt-ae4000",
                                        "power": "unknown",
                                        "fault": "I couldn't reach the projector."}
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["projector"]["fault"],
                         "I couldn't reach the projector.")

    def test_a_notice_carries_a_failure_the_post_could_not(self):
        """A preparation that gives up does so on a thread, long after the POST
        that started it answered 200. Without this the grid simply comes back
        and the poster looks broken."""
        self.projectionist.message = "The movie would not start."
        body = self.assertJson(self.fetch("/api/status"))
        self.assertEqual(body["notice"], "The movie would not start.")

    def test_there_is_no_notice_when_nothing_went_wrong(self):
        self.assertEqual(self.assertJson(self.fetch("/api/status"))["notice"], "")


def _elsewhere():
    """An allow-list that cannot contain the loopback address the tests come
    from, so the filter is exercised rather than merely configured."""
    return [ipaddress.ip_network("10.99.0.0/16")]


def _explode():
    raise AssertionError("airplay_active() should not have been called")


if __name__ == "__main__":
    unittest.main()
