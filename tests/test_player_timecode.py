"""Player._advance(): what the daemon publishes as the film's clock.

Every listening phone reads its audio position out of this, so the questions
worth asking are about honesty rather than about behaviour. When was the
reading true? Is this still the same timeline it was a moment ago? And is the
anchor the best of the readings available or merely the newest?

The real Player is driven here with mpv replaced by a dict of properties and
time replaced by a variable, because the two things under test -- an instant
and a jump -- are both invisible against a wall clock.
"""

import unittest

from support import player as player_mod     # the real one, not a fake


class FakeClock:
    """time.monotonic() as a number the test moves.

    Deliberately not a monotonically-increasing real clock: half of what is
    being checked is the width of the interval a reading was taken inside, and
    that is nanoseconds on a real machine and whatever this file says here.
    """

    def __init__(self, t=918273.0):
        self.t = t
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


class Rig(unittest.TestCase):
    """A Player with mpv and the clock taken out.

    _advance() is called directly rather than through status(): status() is
    what SAMPLES, and every question here is about what is done with a sample
    once it exists. The path from mpv to _advance is covered by Sampling below.
    """

    def setUp(self):
        self.player = player_mod.Player()
        self.clock = FakeClock()

    def advance(self, pos, at=None, paused=False, buffering=False):
        return self.player._advance(pos, self.clock.t if at is None else at,
                                    paused, buffering)


class Shape(Rig):
    def test_a_timecode_says_where_when_whether_and_which(self):
        tc = self.advance(10.0)
        self.assertEqual(set(tc), {"tc", "at", "rate", "epoch"})
        self.assertAlmostEqual(tc["tc"], 10.0)
        self.assertAlmostEqual(tc["at"], self.clock.t)
        self.assertEqual(tc["rate"], 1.0)
        self.assertEqual(tc["epoch"], 1)

    def test_a_film_mpv_has_not_opened_yet_has_no_timecode(self):
        """None rather than zero. During the first second of a film "I do not
        know yet" and "the beginning" are different answers, and a phone that
        believed the second one would seek to the start of the film."""
        self.assertIsNone(self.advance(None))

    def test_the_caller_is_handed_a_copy(self):
        """The published dict travels through JSON to six phones. A caller that
        could mutate the daemon's own copy would be editing what the next one
        is told."""
        tc = self.advance(10.0)
        tc["tc"] = 999.0
        self.clock.advance(0.5)
        self.assertLess(self.advance(10.5)["tc"], 100.0)


class Rate(Rig):
    def test_a_playing_film_runs_at_one(self):
        self.assertEqual(self.advance(10.0)["rate"], 1.0)

    def test_a_paused_film_does_not_run_at_all(self):
        self.assertEqual(self.advance(10.0, paused=True)["rate"], 0.0)

    def test_nor_does_one_whose_demuxer_is_waiting_on_the_nas(self):
        """mpv freezes the picture and stops advancing time-pos while the cache
        refills, which over CIFS on this box's Wi-Fi is a thing that happens. A
        phone that kept playing through it is permanently ahead afterwards."""
        self.assertEqual(self.advance(10.0, buffering=True)["rate"], 0.0)

    def test_a_stopped_timeline_evaluates_to_where_it_stopped(self):
        """The reason rate is a number rather than a flag: a phone applies one
        formula, and a rate of zero makes it answer the frozen position without
        a branch anywhere."""
        tc = self.advance(10.0, paused=True)
        later = tc["tc"] + tc["rate"] * (tc["at"] + 30.0 - tc["at"])
        self.assertEqual(later, 10.0)


class Epoch(Rig):
    def test_an_ordinary_advance_keeps_the_timeline(self):
        first = self.advance(10.0)["epoch"]
        for _ in range(5):
            self.clock.advance(0.5)
            tc = self.advance(10.0 + 0.5 * (_ + 1))
        self.assertEqual(tc["epoch"], first)

    def test_a_pause_is_a_new_timeline(self):
        first = self.advance(10.0)["epoch"]
        self.clock.advance(0.5)
        self.assertEqual(self.advance(10.5, paused=True)["epoch"], first + 1)

    def test_and_so_is_the_resume(self):
        """Both directions, because a phone cannot know WHEN the resume
        happened -- only that between two polls the film started moving again,
        which is a place it has to be put rather than nudged toward."""
        self.advance(10.0)
        self.clock.advance(0.5)
        paused = self.advance(10.5, paused=True)["epoch"]
        self.clock.advance(30.0)
        self.assertEqual(self.advance(10.5)["epoch"], paused + 1)

    def test_a_position_that_is_not_where_it_should_be_is_a_new_timeline(self):
        """The safety net, and the only rule here that catches something this
        process did not do itself. mpv seeking, looping, or being driven from
        its own IPC socket by somebody debugging all look like this."""
        first = self.advance(10.0)["epoch"]
        self.clock.advance(1.0)
        self.assertEqual(self.advance(400.0)["epoch"], first + 1)

    def test_a_position_that_is_roughly_where_it_should_be_is_not(self):
        first = self.advance(10.0)["epoch"]
        self.clock.advance(1.0)
        # Inside TIMECODE_JUMP: frame quantisation and IPC latency live here,
        # and treating either as a discontinuity would make every phone seek
        # once a second.
        self.assertEqual(self.advance(11.0 - 0.2)["epoch"], first)

    def test_the_counter_only_ever_goes_up(self):
        """A film ending drops the anchor and keeps the number. "The timeline
        you were following is gone" and "here is a different one" are the same
        message to a phone, and it must not be possible to send it twice by
        accident -- which is what reusing an epoch would be."""
        seen = []
        for _ in range(3):
            self.clock.advance(1.0)
            seen.append(self.advance(10.0)["epoch"])
            self.player._timecode = None      # what _teardown() does
            self.player._window = []
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(set(seen)), 3)


class Anchor(Rig):
    """mpv reports time-pos on frame boundaries, so a reading is up to a frame
    behind the truth and never ahead of it. That is a one-sided error, which is
    the kind a maximum removes -- and doing it here means it is done once for
    every listener rather than once per phone over data the network has already
    added noise to.
    """

    def feed(self, samples):
        """(elapsed, reported) pairs, played out against the fake clock."""
        tc = None
        for elapsed, reported in samples:
            self.clock.advance(elapsed)
            tc = self.advance(reported)
        return tc

    def test_the_least_late_reading_wins_over_the_newest(self):
        # A film that really is at 10.0 + elapsed, read through a 42 ms frame
        # staircase. The second sample is the honest one; the last is 40 ms
        # late, and a model that simply took the newest would inherit that.
        tc = self.feed([(0.0, 10.000), (0.5, 10.498), (0.5, 10.960)])
        line = tc["tc"] + (self.clock.t - tc["at"])
        self.assertAlmostEqual(line, 11.0, delta=0.005)

    def test_a_single_reading_is_still_answered(self):
        """Nothing waits for a population here. The reading may be a frame
        late, and a frame is 42 ms against a perception threshold of 45 -- the
        window sharpens that, it is not what makes it usable."""
        tc = self.advance(10.0)
        self.assertAlmostEqual(tc["tc"], 10.0)

    def test_the_window_does_not_grow_without_bound(self):
        for i in range(50):
            self.clock.advance(0.5)
            self.advance(10.0 + 0.5 * i)
        self.assertLessEqual(len(self.player._window),
                             player_mod.TIMECODE_WINDOW)

    def test_a_reading_nobody_asked_for_in_a_while_stops_being_an_anchor(self):
        """Samples are only taken when something asks for a status, so with one
        poller every few seconds -- a curl, a dashboard, nobody at all -- eight
        of them can span minutes. mpv's time-pos is paced by the audio clock
        rather than by this machine's monotonic one, and a winner held across
        that long a gap is tens of milliseconds stale by the end of it."""
        self.advance(10.0)
        self.clock.advance(60.0)
        tc = self.advance(70.0)
        self.assertEqual(len(self.player._window), 1)
        self.assertAlmostEqual(tc["at"], self.clock.t)

    def test_a_slow_poller_does_not_start_a_new_timeline_though(self):
        """Ageing a sample out of the anchor window and calling the timeline
        broken are different things, and confusing them would make every phone
        seek whenever the house went quiet for a minute."""
        first = self.advance(10.0)["epoch"]
        self.clock.advance(60.0)
        self.assertEqual(self.advance(70.0)["epoch"], first)

    def test_a_new_timeline_throws_the_window_away(self):
        """Readings from before a seek describe a different film position and
        would win the maximum for the next few seconds if they were kept."""
        self.feed([(0.0, 10.0), (0.5, 10.5), (0.5, 11.0)])
        self.clock.advance(0.5)
        self.advance(400.0)
        self.assertEqual(len(self.player._window), 1)

    def test_a_stopped_timeline_anchors_on_the_reading_itself(self):
        """A maximum over readings taken while the film was moving would be
        extrapolated by a phone at a rate the film is no longer running at."""
        self.feed([(0.0, 10.0), (0.5, 10.5)])
        self.clock.advance(0.5)
        tc = self.advance(11.0, paused=True)
        self.assertAlmostEqual(tc["tc"], 11.0)
        self.assertAlmostEqual(tc["at"], self.clock.t)
        self.assertEqual(self.player._window, [])


class Sampling(unittest.TestCase):
    """status(), with mpv replaced by a dict and every read timestamped.

    The bug this half exists to prevent is the one that was here: the position
    was stamped with a clock read AFTER four more IPC round trips, and every
    listener inherited however long mpv took to answer them.
    """

    def setUp(self):
        self.player = player_mod.Player()
        self.props = {"time-pos": 10.0, "duration": 100.0, "pause": False,
                      "paused-for-cache": False, "volume": 50}
        self.reads = []
        self.now = [918273.0]

        def get_property(name):
            # Every read costs time, and time-pos is not the slowest of them.
            self.reads.append(name)
            self.now[0] += 0.010
            return self.props.get(name)

        self.player.get_property = get_property
        self.player._proc = _AliveProc()
        self.addCleanup(setattr, self.player, "_proc", None)
        self._real_monotonic = player_mod.time.monotonic
        player_mod.time.monotonic = lambda: self.now[0]
        self.addCleanup(setattr, player_mod.time, "monotonic",
                        self._real_monotonic)

    def test_the_instant_is_the_one_time_pos_was_read_at(self):
        data = self.player.status()
        at = data["timecode"]["at"]
        # time-pos is read first, so the midpoint of its own call is 5 ms in.
        # A stamp taken after the other four reads would be 45 ms in, and a
        # phone would place its audio 40 ms early with nothing to say so.
        self.assertAlmostEqual(at, 918273.005, places=6)

    def test_time_pos_is_read_before_anything_else(self):
        self.player.status()
        self.assertEqual(self.reads[0], "time-pos")

    def test_the_position_is_not_extrapolated_into_the_timecode(self):
        """The timecode says when. Carrying it forward as well would be the
        daemon guessing at a clock it does not have -- the phone's."""
        data = self.player.status()
        self.assertAlmostEqual(data["timecode"]["tc"], 10.0)

    def test_the_position_field_is_still_moved_to_now(self):
        """...for the progress bar, which is the only thing that reads it and
        would otherwise step half a second at a time."""
        self.player.status()
        self.now[0] += 0.4
        data = self.player.status()          # inside the cache window
        self.assertGreater(data["position"], 10.3)
        self.assertLess(data["position"], 10.6)

    def test_a_paused_film_does_not_invent_progress(self):
        self.props["pause"] = True
        self.player.status()
        self.now[0] += 5.0
        self.assertAlmostEqual(self.player.status()["position"], 10.0)

    def test_nothing_is_asked_of_mpv_inside_the_cache_window(self):
        self.player.status()
        count = len(self.reads)
        self.player.status()
        self.assertEqual(len(self.reads), count)

    def test_a_dead_player_has_nothing_to_say(self):
        self.player._proc = None
        self.assertEqual(self.player.status(), {})


class Seeking(unittest.TestCase):
    """seek(), with mpv replaced by a dict of properties and a command log.

    Two things are being checked and neither is about arithmetic. The first is
    the clamp at the far end: mpv runs with --keep-open=no, so a seek to the
    very end of a film is the film ENDING, and a child holding the forward
    button must not be able to make that happen. The second is the epoch --
    every listening phone re-places its headphones when the epoch moves, and a
    seek that did not move it would leave the whole room out of sync with the
    picture and nothing on the wire to say why.
    """

    def setUp(self):
        self.player = player_mod.Player()
        self.props = {"time-pos": 600.0, "duration": 5400.0, "pause": False,
                      "paused-for-cache": False, "volume": 50, "sid": False}
        self.commands = []
        self.player.get_property = lambda name: self.props.get(name)
        self.player._proc = _AliveProc()
        self.addCleanup(setattr, self.player, "_proc", None)
        self.now = [918273.0]
        self._real_monotonic = player_mod.time.monotonic
        self._real_time = player_mod.time.time
        self._real_sleep = player_mod.time.sleep
        player_mod.time.monotonic = lambda: self.now[0]
        player_mod.time.time = lambda: self.now[0]
        player_mod.time.sleep = lambda dt: self.now.__setitem__(0, self.now[0] + dt)
        self.addCleanup(setattr, player_mod.time, "monotonic", self._real_monotonic)
        self.addCleanup(setattr, player_mod.time, "time", self._real_time)
        self.addCleanup(setattr, player_mod.time, "sleep", self._real_sleep)

        def command(cmd, quiet=False):
            self.commands.append(cmd)
            # mpv, landing where it was told to. The real one answers "queued"
            # and arrives a moment later, which is what the wait exists for.
            if cmd[0] == "seek":
                self.props["time-pos"] = cmd[1]
            return {"error": "success"}

        self.player.command = command

    def test_a_skip_is_an_absolute_seek_to_where_it_should_land(self):
        """Absolute rather than relative, and that is the difference between a
        control that works and one that nearly does: mpv seeks relatively to
        the nearest keyframe, so a ten-second nudge would land anywhere on the
        GOP. The page promises ten."""
        self.player.seek(10)
        self.assertEqual(self.commands, [["seek", 610.0, "absolute"]])

    def test_it_waits_for_the_seek_to_land_before_answering(self):
        """The route answers with a status, and mpv's reply to a seek means
        queued rather than done. Without the wait the page would be told the
        position it had before it pressed the button."""
        self.assertAlmostEqual(self.player.seek(-100), 500.0)
        self.assertAlmostEqual(self.props["time-pos"], 500.0)

    def test_a_forward_skip_stops_short_of_the_end(self):
        self.player.seek(9999)
        self.assertEqual(self.commands[-1], ["seek", 5395.0, "absolute"])

    def test_a_backward_skip_stops_at_the_start(self):
        self.player.seek(-9999)
        self.assertEqual(self.commands[-1], ["seek", 0.0, "absolute"])

    def test_a_film_with_no_duration_yet_is_still_seekable(self):
        """The first seconds of a film, before mpv has parsed a duration. The
        near clamp still applies; the far one has nothing to clamp to."""
        self.props["duration"] = 0
        self.player.seek(30)
        self.assertEqual(self.commands[-1], ["seek", 630.0, "absolute"])

    def test_a_player_that_does_not_know_where_it_is_does_not_guess(self):
        self.props["time-pos"] = None
        self.assertIsNone(self.player.seek(10))
        self.assertEqual(self.commands, [])

    def test_the_epoch_moves_so_every_phone_re_places_its_audio(self):
        before = self.player.status()["timecode"]["epoch"]
        self.player.seek(600)
        after = self.player.status()["timecode"]["epoch"]
        self.assertGreater(after, before)
        self.assertAlmostEqual(self.player.status()["timecode"]["tc"], 1200.0)

    def test_the_status_cache_does_not_outlive_a_seek(self):
        """Half a second of cache is what keeps six phones from asking mpv
        twenty times. It must not be what keeps the phone that just pressed a
        button from seeing the result."""
        self.player.status()
        self.player.seek(120)
        self.assertAlmostEqual(self.player.status()["position"], 720.0)


class Subtitles(unittest.TestCase):
    """sub_tracks() and set_subtitle(): what mpv has, and which of it is on.

    Asked of mpv rather than counted off the --sub-file arguments, because a
    container can carry text subtitle tracks of its own and mpv numbers
    everything it loaded together.
    """

    TRACKS = [
        {"id": 1, "type": "video"},
        {"id": 1, "type": "sub", "lang": "eng", "title": "English",
         "external": True, "external-filename": "/lib/.playstick/subs/x.eng.srt"},
        {"id": 2, "type": "sub", "external": True,
         "external-filename": "/lib/.playstick/subs/x.fin.srt"},
        {"id": 3, "type": "sub", "lang": "swe", "title": "Svenska"},
    ]

    def setUp(self):
        self.player = player_mod.Player()
        self.props = {"track-list": self.TRACKS}
        self.lookups = []
        self.commands = []

        def get_property(name):
            self.lookups.append(name)
            return self.props.get(name)

        self.player.get_property = get_property
        self.player.command = lambda cmd, quiet=False: self.commands.append(cmd)
        self.player._item = {
            "id": "x",
            "subs": [{"path": "/lib/.playstick/subs/x.eng.srt", "lang": "eng"},
                     {"path": "/lib/.playstick/subs/x.fin.srt", "lang": "fin"}],
        }

    def test_only_the_subtitle_tracks_come_back(self):
        self.assertEqual([t["n"] for t in self.player.sub_tracks()], [1, 2, 3])

    def test_the_index_supplies_a_language_mpv_did_not_guess(self):
        """mpv guesses an external file's language from its name. Prep's naming
        happens to be guessable, but a feature that stops working because a
        filename convention changed is not one worth shipping -- so the index's
        own answer wins where there is one."""
        self.assertEqual([t["lang"] for t in self.player.sub_tracks()],
                         ["eng", "fin", "swe"])

    def test_a_track_from_the_container_keeps_mpvs_own_answer(self):
        embedded = self.player.sub_tracks()[2]
        self.assertEqual((embedded["lang"], embedded["title"]), ("swe", "Svenska"))

    def test_mpv_is_asked_once_per_film(self):
        self.player.sub_tracks()
        self.player.sub_tracks()
        self.assertEqual(self.lookups.count("track-list"), 1)

    def test_a_file_mpv_has_not_loaded_yet_is_not_cached_as_empty(self):
        """The first second of a film. "No subtitles" and "not asked yet" are
        different answers and only one of them is final."""
        self.props["track-list"] = None
        self.assertEqual(self.player.sub_tracks(), [])
        self.props["track-list"] = self.TRACKS
        self.assertEqual(len(self.player.sub_tracks()), 3)

    def test_choosing_a_track_sets_sid(self):
        self.assertEqual(self.player.set_subtitle(2), 2)
        self.assertEqual(self.commands[-1], ["set_property", "sid", 2])

    def test_zero_turns_them_off_by_name(self):
        self.player.set_subtitle(0)
        self.assertEqual(self.commands[-1], ["set_property", "sid", "no"])

    def test_a_track_nobody_loaded_never_reaches_mpv(self):
        self.assertIsNone(self.player.set_subtitle(9))
        self.assertEqual(self.commands, [])


class _AliveProc:
    def poll(self):
        return None


if __name__ == "__main__":
    unittest.main()
