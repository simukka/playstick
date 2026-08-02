"""/api/audio -- the only route in the process that streams file bytes.

It is also the only one a client other than our own page ever talks to, and
the client in question is iOS Safari, which is particular. Two of its habits
drive most of what is tested here: it opens every media resource with
`Range: bytes=0-1` and refuses the resource outright if the answer is a 200
rather than a 206, and it pulls a progressive file as fast as the socket will
go -- across the one Wi-Fi radio that is simultaneously reading the film off
the NAS. Hence the range handling and hence the pacing.
"""

import os
import threading
import time
import unittest

from support import ApiTest, api, audio_track, patched, write_file


FILM = "0123456789abcdef"
OTHER = "fedcba9876543210"
DATA = bytes(range(256)) * 32           # 8192 bytes, and every offset distinct


class AudioTest(ApiTest):
    """A film with two prepared tracks, both of them real files on disk. The
    handler opens and stats them for real -- that is the point of the fixture
    being a file rather than a mock, since Content-Length, the ETag and every
    range bound are computed from the stat."""

    def setUp(self):
        super().setUp()
        self.jpn = write_file("tracks/%s.0.m4a" % FILM, DATA)
        self.eng = write_file("tracks/%s.1.m4a" % FILM, b"english " * 64)
        self.library.add(FILM, "Ponyo", audio=[
            audio_track(0, "jpn", "Japanese", path=self.jpn),
            audio_track(1, "eng", "English", path=self.eng),
        ])

    def url(self, ident=FILM, track=0):
        return "/api/audio/%s/%s" % (ident, track)

    def etag(self, path):
        stat = os.stat(path)
        return '"%x-%x"' % (stat.st_size, stat.st_mtime_ns)

    def get(self, track=0, headers=None, method="GET", **kwargs):
        return self.fetch(self.url(track=track), method=method,
                          headers=headers, **kwargs)


class Route(AudioTest):
    def test_a_whole_track_is_served(self):
        resp = self.get()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, DATA)
        self.assertEqual(resp.header("Content-Type"), "audio/mp4")
        self.assertEqual(resp.header("Content-Length"), str(len(DATA)))
        self.assertEqual(resp.header("Accept-Ranges"), "bytes")
        self.assertEqual(resp.header("ETag"), self.etag(self.jpn))

    def test_the_second_track_is_a_different_track(self):
        """Two people watching the same film in different languages is the
        whole feature. The number indexes this film's list and nothing else."""
        self.assertEqual(self.get(track=1).body, b"english " * 64)

    def test_the_cache_header_keeps_it_off_any_proxy(self):
        """One household's film. Private, and an hour rather than a year --
        re-running prep replaces these files in place."""
        self.assertEqual(self.get().header("Cache-Control"),
                         "private, max-age=3600")

    def test_an_unknown_film_is_not_found(self):
        self.assertNotFound(self.fetch(self.url(ident=OTHER)))

    def test_a_track_number_past_the_end_is_not_found(self):
        for track in [2, 9, 99]:
            with self.subTest(track=track):
                self.assertNotFound(self.get(track=track))

    def test_a_film_nobody_prepped_has_no_tracks_to_ask_for(self):
        self.library.add(OTHER, "Arrietty")
        self.assertNotFound(self.fetch(self.url(ident=OTHER)))

    def test_a_track_the_share_lost_is_a_404_and_not_a_traceback(self):
        """Listed in the index, gone from the NAS -- somebody re-prepped the
        library, or the share went away mid-film. The page can put a 404 into
        words."""
        self.library.items[FILM]["audio"][0]["path"] = "/srv/movies/gone.m4a"
        body = self.assertJson(self.get(), 404)
        self.assertEqual(body, {"error": "That soundtrack is not on the share."})
        # And it does not name the path it failed to open.
        self.assertNotIn("/srv/movies", str(body))

    def test_a_build_without_phone_audio_does_not_serve_audio(self):
        with patched(PHONE_AUDIO=False):
            self.assertNotFound(self.get())
            self.assertEqual(self.get(method="HEAD").status, 404)

    def test_the_route_is_matched_whole_rather_than_sliced(self):
        """The load-bearing difference from the thumbnail route, which slices
        the URL and relies on the result being a dict key. Here the regex is
        anchored at both ends over sixteen lowercase hex and one or two
        digits, so none of these is a request at all."""
        for path in [
            "/api/audio/../../etc/passwd",
            "/api/audio/%2e%2e/%2e%2e/etc/passwd",
            "/api/audio/" + FILM.upper() + "/0",        # uppercase hex
            "/api/audio/" + FILM[:15] + "/0",           # fifteen characters
            "/api/audio/" + FILM + "a/0",               # seventeen
            "/api/audio/" + FILM + "/000",              # three-digit track
            "/api/audio/" + FILM + "/-1",
            "/api/audio/" + FILM + "/0/",
            "/api/audio/" + FILM + "/0/../0",
            "/api/audio/" + FILM,
            "/api/audio/" + FILM + "/x",
            "/api/audio//0",
        ]:
            with self.subTest(path=path):
                self.assertNotFound(self.fetch(path))

    def test_a_query_string_does_not_defeat_the_match(self):
        """The page appends one to force a reload when a listener switches
        language. urlparse strips it before the regex sees the path."""
        resp = self.fetch(self.url() + "?t=1712345678")
        self.assertEqual(resp.status, 200)


class Ranges(AudioTest):
    def test_the_two_byte_probe_safari_opens_with(self):
        """iOS Safari's first request for any media resource. A 200 here is
        not a fallback, it is a refusal -- the element never plays."""
        resp = self.get(headers={"Range": "bytes=0-1"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[:2])
        self.assertEqual(resp.header("Content-Length"), "2")
        self.assertEqual(resp.header("Content-Range"),
                         "bytes 0-1/%d" % len(DATA))

    def test_a_range_in_the_middle(self):
        resp = self.get(headers={"Range": "bytes=100-199"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[100:200])
        self.assertEqual(resp.header("Content-Range"),
                         "bytes 100-199/%d" % len(DATA))

    def test_an_open_ended_range_runs_to_the_end(self):
        """What a media element sends when it resumes after a seek."""
        resp = self.get(headers={"Range": "bytes=8000-"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[8000:])
        self.assertEqual(resp.header("Content-Range"),
                         "bytes 8000-8191/%d" % len(DATA))

    def test_an_end_past_the_end_is_clamped(self):
        resp = self.get(headers={"Range": "bytes=8000-999999"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[8000:])
        self.assertEqual(resp.header("Content-Range"),
                         "bytes 8000-8191/%d" % len(DATA))

    def test_a_suffix_range_is_the_last_n_bytes(self):
        """Safari asks for the tail of an mp4 when it wants the moov atom --
        which a +faststart file does not make it need, but two lines is
        cheaper than finding out on a phone."""
        resp = self.get(headers={"Range": "bytes=-16"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[-16:])
        self.assertEqual(resp.header("Content-Range"),
                         "bytes 8176-8191/%d" % len(DATA))

    def test_a_suffix_longer_than_the_file_is_the_whole_file(self):
        resp = self.get(headers={"Range": "bytes=-999999"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA)

    def test_a_whole_file_range_is_still_partial(self):
        resp = self.get(headers={"Range": "bytes=0-"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA)

    def test_a_start_past_the_end_is_unsatisfiable(self):
        for span in ["bytes=8192-", "bytes=8192-9000", "bytes=99999-", "bytes=-0"]:
            with self.subTest(span=span):
                resp = self.get(headers={"Range": span})
                self.assertEqual(resp.status, 416)
                self.assertEqual(resp.header("Content-Range"),
                                 "bytes */%d" % len(DATA))
                self.assertEqual(resp.body, b"")

    def test_a_backwards_range_is_unsatisfiable(self):
        resp = self.get(headers={"Range": "bytes=200-100"})
        self.assertEqual(resp.status, 416)

    def test_a_range_header_that_says_nothing_is_unsatisfiable(self):
        for span in ["bytes=", "bytes=-", "bytes=abc", "items=0-1", "0-1",
                     "bytes=0-1, 4-8"]:
            with self.subTest(span=span):
                self.assertEqual(self.get(headers={"Range": span}).status, 416)

    def test_surrounding_whitespace_is_tolerated(self):
        resp = self.get(headers={"Range": "  bytes=0-1  "})
        self.assertEqual(resp.status, 206)

    def test_no_range_header_is_a_plain_200_with_no_content_range(self):
        resp = self.get()
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.header("Content-Range"))


class ConditionalRanges(AudioTest):
    """If-Range is what stops a phone that cached the first half of a track it
    then re-requests from being handed bytes out of a different file at the
    same offsets -- which is exactly what re-running prep produces."""

    def test_a_matching_validator_honours_the_range(self):
        resp = self.get(headers={"Range": "bytes=100-199",
                                 "If-Range": self.etag(self.jpn)})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[100:200])

    def test_a_stale_validator_ignores_the_range_rather_than_failing(self):
        """A mismatch means 'forget the Range', not 'fail'. The client gets
        the whole resource and sorts itself out."""
        resp = self.get(headers={"Range": "bytes=100-199",
                                 "If-Range": '"deadbeef-1"'})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, DATA)
        self.assertIsNone(resp.header("Content-Range"))

    def test_a_stale_validator_also_defuses_an_unsatisfiable_range(self):
        """The Range is discarded before it is parsed, so nothing about it can
        turn into a 416 the client has no way to interpret."""
        resp = self.get(headers={"Range": "bytes=99999-",
                                 "If-Range": '"deadbeef-1"'})
        self.assertEqual(resp.status, 200)

    def test_the_etag_changes_when_the_file_does(self):
        """Size and mtime, so a re-prepped track is a different resource even
        if it happens to be the same length."""
        before = self.get().header("ETag")
        time.sleep(0.01)
        with open(self.jpn, "wb") as fh:
            fh.write(DATA)                     # same bytes, new mtime
        self.assertNotEqual(self.get().header("ETag"), before)


class Head(AudioTest):
    """Answered only on this route, and only because media clients and anybody
    debugging one reach for HEAD first."""

    def test_head_gives_the_headers_of_the_get(self):
        conn = self.connect()
        head = self.fetch(self.url(), method="HEAD", conn=conn)
        self.assertEqual(head.status, 200)
        self.assertEqual(head.header("Content-Length"), str(len(DATA)))
        self.assertEqual(head.header("Content-Type"), "audio/mp4")
        self.assertEqual(head.header("Accept-Ranges"), "bytes")
        self.assertEqual(head.body, b"")

    def test_head_honours_a_range(self):
        resp = self.fetch(self.url(), method="HEAD",
                          headers={"Range": "bytes=0-1"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.header("Content-Range"),
                         "bytes 0-1/%d" % len(DATA))
        self.assertEqual(resp.header("Content-Length"), "2")

    def test_head_writes_the_headers_and_stops(self):
        """Content-Length announces the body without one being sent -- which
        is the whole of what a media client asks HEAD for. Read off the socket
        because http.client cannot tell a suppressed body from a discarded
        one."""
        sock = self.raw(b"HEAD %s HTTP/1.1\r\nHost: x\r\n\r\n" % self.url().encode())
        wire = self.drain(sock)
        self.assertIn(b"200 OK", wire.split(b"\r\n", 1)[0])
        self.assertIn(b"Content-Length: %d" % len(DATA), wire)
        self.assertTrue(wire.endswith(b"\r\n\r\n"),
                        "%d bytes of body followed the headers"
                        % len(wire.split(b"\r\n\r\n", 1)[1]))

    def test_head_on_a_missing_track_writes_no_body_either(self):
        """The 404 goes through _send, which is shared with every other route
        on the server -- so this is where a regression in the common helper
        would show up first."""
        sock = self.raw(b"HEAD %s HTTP/1.1\r\nHost: x\r\n\r\n"
                        % self.url(track=9).encode())
        wire = self.drain(sock)
        self.assertIn(b"404", wire.split(b"\r\n", 1)[0])
        self.assertTrue(wire.endswith(b"\r\n\r\n"),
                        "a body followed the headers: %r" % wire[-80:])

    def test_the_connection_is_reusable_after_a_head(self):
        conn = self.connect()
        self.fetch(self.url(), method="HEAD", conn=conn)
        resp = self.fetch(self.url(), headers={"Range": "bytes=0-1"}, conn=conn)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, DATA[:2])


class Slots(AudioTest):
    """Each listener holds a thread for as long as they listen. Refusing the
    seventh is better than starving the page every phone is polling."""

    def test_a_stream_gives_its_slot_back(self):
        with patched(AUDIO_SLOTS=threading.Semaphore(1)):
            self.assertEqual(self.get().status, 200)
            # Would be a 503 if the first response had leaked the slot.
            self.assertEqual(self.get().status, 200)

    def test_a_404_gives_its_slot_back(self):
        """The acquire happens after the lookup, so a miss must never consume
        one -- a phone retrying a track that is not there would otherwise lock
        every listener out."""
        with patched(AUDIO_SLOTS=threading.Semaphore(1)):
            self.assertNotFound(self.get(track=9))
            self.assertEqual(self.get().status, 200)

    def test_the_listener_over_the_limit_is_told_to_come_back(self):
        with patched(AUDIO_SLOTS=threading.Semaphore(0)):
            resp = self.get()
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.header("Content-Type"), "application/json")
        self.assertEqual(resp.header("Retry-After"), "2")
        self.assertEqual(resp.json, {"error": "too many listeners"})

    def test_a_slot_is_held_for_as_long_as_somebody_is_listening(self):
        """The one that needs a real stream: a paced response that is still
        being written must be occupying the semaphore, and must free it when
        the listener walks out of Wi-Fi range."""
        slots = threading.Semaphore(1)
        # Slow enough that the first stream cannot finish, fine-grained enough
        # that it notices a dropped socket within a chunk's worth of pacing.
        with patched(AUDIO_SLOTS=slots, PHONE_AUDIO_BPS=2048,
                     PHONE_AUDIO_BURST=0, AUDIO_CHUNK=256):
            sock = self.raw(b"GET %s HTTP/1.1\r\nHost: x\r\n\r\n"
                            % self.url().encode())
            self.assertIn(b"200 OK", _await(sock))

            self.assertEqual(self.get().status, 503)

            # Somebody locks their phone, or walks out of range.
            sock.close()
            self.assertTrue(_eventually(lambda: self.get().status == 200),
                            "the slot was never released")


class Pacing(AudioTest):
    """Unpaced, a phone choosing a language is indistinguishable from the film
    stuttering: every byte crosses this box's single SDIO-attached radio twice,
    alongside the film's own CIFS read.
    """

    def test_the_chunk_size_is_the_delivery_granularity(self):
        """An arithmetic invariant rather than a measurement, and the reason
        AUDIO_CHUNK is 8 KiB and not 64. The pacing loop writes a chunk and
        then sleeps off what that chunk owes, so the chunk size decides how
        lumpy the stream is: at the default 384 kbps, 64 KiB meant one write
        every 1.37 s, which is a listener reporting dropouts.
        """
        default_bps = 384 * 1000 // 8
        self.assertLessEqual(api.AUDIO_CHUNK / default_bps, 0.25)

    def test_a_paced_stream_arrives_in_pieces_rather_than_lumps(self):
        """Timing-sensitive by nature, so the bounds are loose: eight chunks
        are expected and four distinct arrivals are asserted. What it would
        catch is the failure that matters -- the whole body turning up in one
        piece after a long silence."""
        with patched(PHONE_AUDIO_BPS=8192, PHONE_AUDIO_BURST=0,
                     AUDIO_CHUNK=1024):
            sock = self.raw(b"GET %s HTTP/1.1\r\nHost: x\r\n\r\n"
                            % self.url().encode())
            arrivals = _read_arrivals(sock, len(DATA))

        body = b"".join(chunk for _at, chunk in arrivals)
        self.assertIn(DATA[-64:], body)
        elapsed = arrivals[-1][0] - arrivals[0][0]
        # 8192 bytes at 8192 B/s with no burst owes about a second.
        self.assertGreater(elapsed, 0.5,
                           "the stream was not paced at all")
        self.assertGreaterEqual(_batches(arrivals), 4,
                                "the body arrived in too few pieces")

    def test_the_burst_hands_over_the_opening_at_full_speed(self):
        """So playback starts at once and has a cushion to survive a hiccup
        with. Only what is past the burst is paced."""
        with patched(PHONE_AUDIO_BPS=1024, PHONE_AUDIO_BURST=30):
            began = time.monotonic()
            resp = self.get()
            elapsed = time.monotonic() - began
        self.assertEqual(resp.body, DATA)
        # 8192 bytes at 1024 B/s would be eight seconds without the burst.
        self.assertLess(elapsed, 2.0)

    def test_pacing_can_be_switched_off(self):
        with patched(PHONE_AUDIO_BPS=0):
            self.assertEqual(self.get().body, DATA)


def _await(sock, marker=b"\r\n\r\n", timeout=5):
    """Read until the end of the response headers."""
    deadline = time.monotonic() + timeout
    buf = b""
    while marker not in buf and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _read_arrivals(sock, want, timeout=20):
    """Every recv, stamped. http.client cannot answer WHEN a body arrived, and
    when is the whole question the pacing raises."""
    header = _await(sock)
    body = header.split(b"\r\n\r\n", 1)[1]
    arrivals = [(time.monotonic(), body)] if body else []
    seen = len(body)
    deadline = time.monotonic() + timeout
    while seen < want and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        arrivals.append((time.monotonic(), chunk))
        seen += len(chunk)
    return arrivals


def _batches(arrivals, gap=0.05):
    """Arrivals separated by a real pause. Consecutive recvs of one write get
    counted once."""
    count = 1
    for (before, _a), (after, _b) in zip(arrivals, arrivals[1:]):
        if after - before > gap:
            count += 1
    return count


def _eventually(check, timeout=5, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return False


if __name__ == "__main__":
    unittest.main()
