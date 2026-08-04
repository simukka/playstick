"""The HTTP surface: one handler, and the routes it will answer.

No filesystem path ever crosses this boundary in either direction -- clients
send opaque ids and small integers, and /api/audio is the only route in the
process that streams file bytes. The package docstring explains how far that
reaches.
"""

import ipaddress
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .config import (
    ALLOW_NETWORKS, AUDIO_CHUNK, AUDIO_ROUTE_RE, AUDIO_SLOTS, HAS_AUDIO,
    PHONE_AUDIO, PHONE_AUDIO_BPS, PHONE_AUDIO_BURST,
    PHONE_AUDIO_STREAMS, RANGE_RE, SYNC_HEADER, SYNC_KEEP_RE, SYNC_MAX,
    SYNC_MAX_RATE, UI_FILE, log
)
from .airplay import airplay_active
from .thumbs import Thumbs, placeholder_svg
from .player import Busy


_sync_lock = threading.Lock()
_sync_window = [0.0, 0]         # start of the current second, lines written in it


def sync_allowed():
    """One second's worth of budget for telemetry lines, shared by everybody.

    Deliberately global rather than per client: the resource being protected is
    one journal on one eMMC, and a limit that reset per address would be no
    limit at all against the case that motivates it.
    """
    now = time.monotonic()
    with _sync_lock:
        if now - _sync_window[0] >= 1.0:
            _sync_window[0] = now
            _sync_window[1] = 0
        if _sync_window[1] >= SYNC_MAX_RATE:
            return False
        _sync_window[1] += 1
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "playstick"
    protocol_version = "HTTP/1.1"

    # A keep-alive connection that goes quiet used to cost nothing, because
    # every response was over in milliseconds. Now that /api/audio holds one
    # open for as long as somebody is listening, a phone that goes out of range
    # mid-film leaves a thread parked on a socket that will never say anything
    # again. Thirty seconds is far longer than any request here legitimately
    # waits between reads.
    timeout = 30

    library = None      # set on the server before serve_forever
    thumbs = None
    player = None

    # BaseHTTPRequestHandler logs every request to stderr, i.e. into the
    # journal, and a page polling once a second per phone would be the only
    # thing in it. Errors still get through via log_error.
    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        if not ALLOW_NETWORKS:
            return True
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(addr in net for net in ALLOW_NETWORKS)

    # -- helpers

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        # A HEAD carries the headers of the GET and none of its body. Writing
        # one anyway is not merely incorrect, it desynchronises the connection:
        # protocol_version is HTTP/1.1, so the socket stays open and the next
        # request on it reads this body as a status line. _stream_audio makes
        # the same check for the same reason.
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode(), "application/json",
                   {"Cache-Control": "no-store"})

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        # Parsed, but not an object: a bare list or number would reach .get()
        # and take the connection down with an AttributeError that no caller
        # sees. Same answer as unparseable -- there is nothing in it.
        return data if isinstance(data, dict) else {}

    # -- state shared by /api/status and /api/library

    def _state(self):
        state = self.player.state()
        if state != "idle":
            return state
        if airplay_active():
            return "airplay"
        _order, _items, available, _at, _err = self.library.snapshot()
        return "idle" if available else "unavailable"

    # -- routes

    def do_GET(self):                                # noqa: N802 - stdlib API
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if path == "/":
            return self._serve_ui()
        if path == "/api/library":
            return self._api_library()
        if path.startswith("/api/thumb/"):
            return self._api_thumb(path[len("/api/thumb/"):])
        if path == "/api/status":
            return self._api_status()
        if PHONE_AUDIO:
            match = AUDIO_ROUTE_RE.match(path)
            if match:
                return self._api_audio(match.group(1), int(match.group(2)))
        return self._json({"error": "not found"}, 404)

    def do_HEAD(self):                               # noqa: N802 - stdlib API
        """Only the audio route, and only because media clients and anybody
        debugging one reach for HEAD first. Everything else on this server is
        generated per request, so answering HEAD for it would mean building the
        body to measure it and then throwing it away."""
        path = urlparse(self.path).path
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if PHONE_AUDIO:
            match = AUDIO_ROUTE_RE.match(path)
            if match:
                return self._api_audio(match.group(1), int(match.group(2)))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):                               # noqa: N802 - stdlib API
        path = urlparse(self.path).path
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if path == "/api/play":
            return self._api_play()
        if path in ("/api/pause", "/api/resume"):
            self.player.set_pause(path.endswith("pause"))
            return self._api_status()
        if path == "/api/stop":
            self.player.stop()
            return self._api_status()
        if path == "/api/volume":
            # Coerced here rather than in the player, because this is where an
            # untrusted number arrives: the player does arithmetic on it, and
            # a delta of "loud" would otherwise be a ValueError in a request
            # thread and a connection the page never gets an answer on.
            try:
                delta = int(self._body().get("delta", 0))
            except (TypeError, ValueError):
                delta = 0
            self.player.nudge_volume(delta)
            return self._api_status()
        if path == "/api/rescan":
            self.library.request_rescan()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _serve_ui(self):
        try:
            with open(UI_FILE, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._send(500, ("UI missing: %s" % exc).encode(), "text/plain")
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _api_library(self):
        order, items, available, scanned_at, error = self.library.snapshot()
        payload = {
            "available": available,
            "error": error,
            "scanned_at": scanned_at,
            "state": self._state(),
            "items": [
                {
                    "id": ident,
                    "title": items[ident]["title"],
                    # A poster from the index is already on disk, so the tile
                    # never has to show a placeholder first.
                    "has_thumb": bool(items[ident].get("poster")) or self.thumbs.have(ident),
                    # Present only when the library came from the index. The
                    # page ignores what it does not recognise, so this is
                    # additive for an older ui.html.
                    "year": items[ident].get("year"),
                    "rating": items[ident].get("rating"),
                    "genres": items[ident].get("genres") or [],
                    # Empty for a walked share, where the page falls back to
                    # the title -- which is what it would have sorted on anyway.
                    "sort_title": items[ident].get("sort_title") or "",
                    # Languages only, not the tracks themselves. The sheet on
                    # the grid offers a PREFERRED language before a film has
                    # started, and to do that it needs to know what the library
                    # has to offer, not which numbered track any one film keeps
                    # it on.
                    "audio_langs": sorted({t["lang"] for t
                                           in items[ident].get("audio") or []}),
                }
                for ident in order
            ],
        }
        self._json(payload)

    def _api_thumb(self, ident):
        item = self.library.get(ident)
        if item is None:
            return self._json({"error": "not found"}, 404)
        # A poster the prep tool made. Not immutable, unlike the extracted
        # frames below: re-running prep can replace it, and a phone that
        # cached it for a year would never find out.
        poster = item.get("poster")
        if poster:
            try:
                with open(poster, "rb") as fh:
                    return self._send(200, fh.read(), "image/jpeg",
                                      {"Cache-Control": "public, max-age=86400"})
            except OSError:
                pass
        cached = Thumbs.cached_path(ident)
        try:
            with open(cached, "rb") as fh:
                body = fh.read()
        except OSError:
            self.thumbs.request(ident)
            # no-store, so the next request after the frame is extracted gets
            # the real thing without the page having to cache-bust.
            return self._send(200, placeholder_svg(item["title"]),
                              "image/svg+xml", {"Cache-Control": "no-store"})
        self._send(200, body, "image/jpeg",
                   {"Cache-Control": "public, max-age=31536000, immutable"})

    def _log_sync(self, state, position, buffering):
        """A listening phone's own account of the last second, into the journal.

        Sent by the page on every status poll while ?debug is in its URL, and
        by nothing otherwise -- which is the whole gate. There is no server-side
        switch because the alternative to logging this is not logging less, it
        is having no way at all to see what an iPhone in another room was doing
        when the sound broke up.

        Logged next to what this daemon believed at the same instant, because
        half the candidate faults are disagreements between the two: mpv's
        position against the element's, and mpv's buffering against the
        element's. A line reading

            sync 192.168.1.42 playing pos=1421.83 buf=0 v=1;id=8f2c;t=612.4;
            st=play;hid=0;ct=1421.79;rs=4;nb=1;ahead=48.2;amin=47.9;err=-38;
            errp=-41;rate=-712;drift=-680;step=0.2;ns=8;rtt=24;trim=0;w=1;
            dw=140;sk=0;wt=0;bf=0;lag=22;ls=0

        says: the phone is 38 ms behind, correcting by 712 ppm, holding 48
        seconds of buffer, and lost no time to stalls. FIELDS --

          id      per page load, so several phones can be told apart
          t       seconds since that page loaded
          st      off (not listening) | idle (no track) | pause | play
          hid     the screen is locked or the page is in the background
          ct      the element's currentTime
          rs      HTMLMediaElement.readyState, 0-4
          nb      buffered ranges; more than one means it has been seeking
          ahead   seconds of audio past the play head, now
          amin    ...and its low-water mark since the previous line
          err     sound minus picture, ms. Negative is sound behind
          errp    the peak of that, signed, since the previous line
          rate    playbackRate as an offset in ppm
          drift   the integrator: this phone's crystal against mpv's, ppm
          step    last jump in the measured clock offset, ms
          ns      offset samples in the window, 0-8
          rtt     last round trip to this daemon, ms
          trim    this listener's manual headphone offset, ms
          w       writes to playbackRate since the previous line
          dw      the largest of them, ppm
          sk      hard seeks
          wt      waiting/stalled events on the element
          bf      polls where mpv reported paused-for-cache
          lag     worst shortfall in the element's own clock, ms
          ls      how many of those exceeded STALL
          tun     controller constants this listener has changed from the
                  debug sheet, "code:value" in the page's display units.
                  Empty on a stock build, and the only thing that makes a
                  capture taken mid-experiment interpretable afterwards.

        The three that settle it: `ahead`/`amin` collapsing means the daemon or
        the radio is starving the element, `w`/`dw` moving with the dropouts
        means this code is re-arming its pipeline, and `lag`/`ls` staying at
        zero through an audible break means neither -- the clock never stopped
        and the interruption is below anything the page can observe.
        """
        blob = self.headers.get(SYNC_HEADER)
        if not blob:
            return
        # An unauthenticated LAN client is writing to this device's journal, so
        # the value is filtered to a known-safe alphabet and truncated before it
        # goes anywhere near a log call -- and is passed as an ARGUMENT rather
        # than interpolated, so that even a '%' that survived could not reach a
        # format string.
        clean = SYNC_KEEP_RE.sub("", blob)[:SYNC_MAX]
        if not clean or not sync_allowed():
            return
        log("sync %s %s pos=%s buf=%d %s", self.client_address[0], state,
            "?" if position is None else "%.2f" % position, int(buffering), clean)

    def _api_status(self):
        state = self._state()
        data = self.player.status()
        item = self.player.current_item()
        position = data.get("position")
        self._log_sync(state, position, bool(data.get("buffering")))
        tracks = []
        if PHONE_AUDIO and item:
            tracks = [
                {"n": t["n"], "lang": t["lang"], "title": t["title"],
                 "channels": t["channels"], "default": t["default"],
                 # Whatever the extracted track's own start turned out to be,
                 # measured by prep rather than assumed. Normally 0.0; the page
                 # adds it so that a container prep could not fully normalise
                 # is still fixable without touching this device.
                 "offset": t["offset"]}
                for t in item.get("audio") or []
            ]
        self._json({
            "state": state,
            # The page cannot build an audio URL without this.
            "id": item["id"] if item else "",
            "title": self.player.current_title(),
            # Unchanged shape, for the progress bar that has always read it.
            "position": position or 0,
            # ...and the distinction the progress bar does not need but a phone
            # syncing to the film does: mpv has not told us where it is yet.
            "position_valid": position is not None,
            "buffering": bool(data.get("buffering")),
            "duration": data.get("duration", 0),
            "volume": data.get("volume"),
            # Does the PROJECTOR have sound. Nothing to do with the phones.
            "audio": HAS_AUDIO,
            # Does this build serve headphone audio at all.
            "phone_audio": PHONE_AUDIO,
            # Empty for a film nobody has run playstick-prep.py over.
            "tracks": tracks,
            "thumbs_pending": self.thumbs.pending(),
        })

    def _api_audio(self, ident, track):
        """One prepared audio track, streamed to one phone's headphones.

        The only endpoint in this process that streams file bytes, and every
        part of it is narrower than it strictly needs to be. See the module
        header for what that buys: the id has already been proved to be sixteen
        hex characters by the route regex and is used only to look up a table,
        the track number only to index a list, and the path that comes out was
        proved to live under the library root when the index was read. Nothing
        the client sent reaches the filesystem.

        Range is not an optimisation here, it is the price of admission. iOS
        Safari opens every media resource with `Range: bytes=0-1` and, if the
        answer is a 200, refuses the resource outright rather than falling back
        to a whole-file fetch.
        """
        item = self.library.get(ident)
        tracks = (item or {}).get("audio") or []
        if item is None or not 0 <= track < len(tracks):
            return self._json({"error": "not found"}, 404)

        path = tracks[track]["path"]
        try:
            stat = os.stat(path)
            handle = open(path, "rb")
        except OSError:
            # Listed in the index but not on the share: somebody re-prepped the
            # library, or the NAS went away. A 404 the page can put into words,
            # not a traceback in the journal.
            log("audio track missing from the share: %s", path)
            return self._json({"error": "That soundtrack is not on the share."}, 404)

        # Each listener holds a thread for as long as they listen. Refusing the
        # seventh is better than starving the page that every phone in the
        # house is polling.
        if not AUDIO_SLOTS.acquire(blocking=False):
            handle.close()
            log("refusing an audio stream: all %d slots busy", PHONE_AUDIO_STREAMS)
            return self._send(503, b'{"error":"too many listeners"}',
                              "application/json", {"Retry-After": "2"})
        try:
            self._stream_audio(handle, stat)
        finally:
            AUDIO_SLOTS.release()
            handle.close()

    def _stream_audio(self, handle, stat):
        size = stat.st_size
        etag = '"%x-%x"' % (size, stat.st_mtime_ns)
        start, end = 0, size - 1
        partial = False

        span = self.headers.get("Range")
        # A phone that cached part of a track it then re-requests must not be
        # handed bytes from a different file at the same offsets, which is the
        # whole job of If-Range. A mismatch means "ignore the Range", not
        # "fail" -- the client gets the whole thing and sorts itself out.
        if span and self.headers.get("If-Range", etag) == etag:
            match = RANGE_RE.match(span.strip())
            if not match or not (match.group(1) or match.group(2)):
                return self._send(416, b"", "application/json",
                                  {"Content-Range": "bytes */%d" % size})
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:
                # A suffix range: the last N bytes. Safari asks for this when
                # it wants the tail of an mp4, which for a +faststart file it
                # normally does not need -- but it is two lines to be correct.
                start = max(0, size - int(match.group(2)))
            if start >= size or start > end:
                return self._send(416, b"", "application/json",
                                  {"Content-Range": "bytes */%d" % size})
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("ETag", etag)
        # Private: this is one household's film, and there is no proxy in the
        # path that should be keeping it.
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()

        if self.command == "HEAD":
            return

        # Never via _send(): that takes a complete body, and two phones holding
        # 80 MB of film soundtrack in this process is most of the RAM on the
        # device.
        handle.seek(start)
        sent = 0
        began = time.monotonic()
        try:
            while sent < length:
                chunk = handle.read(min(AUDIO_CHUNK, length - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
                if PHONE_AUDIO_BPS:
                    # A token bucket with a burst rather than a hard cap. The
                    # first PHONE_AUDIO_BURST seconds of audio go out as fast as
                    # the socket will take them, so playback starts at once and
                    # has a cushion; after that the stream is paced. What is
                    # being protected is the film's own CIFS read, which crosses
                    # the same radio in the other direction and which nobody can
                    # do anything about once it starts stuttering.
                    #
                    # Slept in short steps and in a LOOP, which is not
                    # decoration. One capped sleep per chunk sounds equivalent
                    # and is not: a chunk owes far more than the cap at any
                    # sensible rate -- 128 KiB at 384 kbps owes 2.7 s -- so a
                    # single capped sleep silently turns the limit into "one
                    # chunk per cap", about 1 MB/s, and the pacing stops pacing
                    # anything. The cap is only here so that a shutdown is not
                    # waiting on a minute-long sleep.
                    due = began + sent / PHONE_AUDIO_BPS - PHONE_AUDIO_BURST
                    while True:
                        owed = due - time.monotonic()
                        if owed <= 0:
                            break
                        time.sleep(min(owed, 0.25))
        except (BrokenPipeError, ConnectionResetError):
            # Somebody switched language, locked their phone, or walked out of
            # Wi-Fi range. All of them look like this and none of them are
            # worth a line in the journal.
            self.close_connection = True

    def _api_play(self):
        ident = str(self._body().get("id", ""))
        item = self.library.get(ident)
        if item is None:
            return self._json({"error": "That film is not in the library."}, 404)
        try:
            self.player.start(item)
        except Busy as exc:
            return self._json({"error": str(exc), "state": self._state()}, 409)
        except Exception as exc:                     # noqa: BLE001
            log("play failed: %s", exc)
            return self._json({"error": "The player would not start."}, 500)
        return self._api_status()
