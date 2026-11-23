"""Getting a film onto a screen that has to be switched on first.

TIME IS A VARIABLE HERE

The sequence waits ten seconds for a documented lamp blackout and up to ninety
for the lamp itself, and the keeper waits thirty minutes before switching
anything off. A suite that sat through any of that is a suite nobody runs, so
the clock and the sleep both go in through the constructor and sleeping is
addition. The same trick tests/js/clock.js uses on the headphone sync
controller, for the same reason -- and it makes the assertions exact rather
than approximate.

THE ONE RULE WORTH BREAKING A BUILD OVER

A projector that cannot be reached must never stop a film playing. Six of the
tests below are that rule from six directions, because it is the property that
makes this feature safe to ship: everything else it does is a convenience, and
convenience is not worth a child in front of a screen that will not play
anything.
"""

import threading
import unittest
from unittest import mock

from support import Clock, FakePlayer, FakeProjector
from playstick.player import Busy
from playstick.projectionist import Projectionist
from playstick import projectionist as module
from playstick.projector.base import NoReply, Refused, Unreachable


FILM = {"id": "0123456789abcdef", "title": "Ponyo", "path": "/srv/movies/Ponyo.mkv"}
OTHER = {"id": "fedcba9876543210", "title": "Totoro", "path": "/srv/movies/T.mkv"}


def settings(**names):
    """Override the module-level configuration for one test.

    Patched into playstick.projectionist rather than playstick.config for the
    same reason support.patched() goes to playstick.http: this module bound the
    names at import with `from .config import ...`, so a patch on config would
    change a value nothing reads.
    """
    names.setdefault("PROJECTOR_INPUT", "HD3")
    return mock.patch.multiple(module, **names)


def no_airplay():
    return mock.patch.multiple(module, airplay_active=lambda: False,
                               airplay_confirmed=lambda: False)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.player = FakePlayer()
        self.projector = FakeProjector()
        self.airplay = no_airplay()
        self.airplay.start()
        self.addCleanup(self.airplay.stop)

    def build(self, **kwargs):
        return Projectionist(self.projector, self.player, clock=self.clock,
                             sleep=self.clock.sleep, **kwargs)

    def run_prepare(self, projectionist, item=FILM, timeout=5):
        """Start a film and wait for the thread to finish, however it ends."""
        prep = projectionist.begin(item)
        self.assertTrue(prep["done"].wait(timeout), "the prepare thread hung")
        return prep


class ColdStart(Base):
    """The whole point: a projector in standby, and a film on it."""

    def test_the_steps_happen_in_order(self):
        steps = []
        self.projector.warmup_polls = 2
        with settings():
            projectionist = self.build()
            # Watching progress() from the outside would race the thread; the
            # order the steps were SET is the thing under test.
            real = projectionist._step
            projectionist._step = lambda prep, name: (steps.append(name),
                                                      real(prep, name))[1]
            self.run_prepare(projectionist)

        self.assertEqual(steps, ["warming", "input", "display", "starting"])
        self.assertEqual(self.player.calls, [("start", FILM["id"])])

    def test_it_asks_before_it_acts(self):
        with settings():
            self.run_prepare(self.build())
        # QPW first: a projector already on must not be sent a PON it would
        # have to interpret.
        self.assertEqual(self.projector.calls[0], "power_state")
        self.assertIn("power_on", self.projector.calls)

    def test_it_waits_out_the_documented_blackout(self):
        with settings(PROJECTOR_WARMUP_SECONDS=10.0):
            self.run_prepare(self.build())
        # Ten seconds of blackout, and no QPW inside it: polling there produces
        # timeouts that say nothing about the cable.
        self.assertGreaterEqual(self.clock.slept, 10.0)

    def test_it_selects_the_configured_input(self):
        with settings(PROJECTOR_INPUT="HD3"):
            self.run_prepare(self.build())
        self.assertIn("set_input:HD3", self.projector.calls)

    def test_no_configured_input_means_the_input_is_left_alone(self):
        """The right setting for a projector that auto-selects its live source,
        and the safe one if the code turns out to be wrong."""
        with settings(PROJECTOR_INPUT=""):
            self.run_prepare(self.build())
        self.assertNotIn("set_input:", " ".join(self.projector.calls))
        self.assertEqual(self.player.calls, [("start", FILM["id"])])

    def test_a_projector_already_on_skips_the_warm_up_entirely(self):
        self.projector.power = "on"
        with settings():
            self.run_prepare(self.build())
        self.assertNotIn("power_on", self.projector.calls)
        self.assertEqual(self.clock.slept, 0.0)


class NoProjector(Base):
    """The default configuration, and the one the development GUI runs."""

    def test_an_unknown_power_state_does_not_wait_for_a_lamp(self):
        """This is the line that keeps an appliance with no serial cable
        behaving exactly as it did before this feature existed. A NullProjector
        answers UNKNOWN forever, and treating that as "not on yet" would make
        every film sit through the full ready timeout first."""
        self.projector.power = "unknown"
        with settings(PROJECTOR_READY_SECONDS=90.0):
            self.run_prepare(self.build())
        self.assertEqual(self.clock.slept, 0.0)
        self.assertNotIn("power_on", self.projector.calls)
        self.assertEqual(self.player.calls, [("start", FILM["id"])])


class BrokenProjector(Base):
    """A projector that cannot be reached must never stop a film."""

    def assert_film_played(self, projectionist):
        self.assertEqual(self.player.calls, [("start", FILM["id"])],
                         "the film must play regardless of the projector")
        self.assertEqual(projectionist.state(), "playing")

    def test_a_port_that_will_not_open(self):
        self.projector.fail["power_state"] = Unreachable("no such device")
        self.projector.fail["power_on"] = Unreachable("no such device")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
        self.assert_film_played(projectionist)

    def test_a_projector_that_does_not_answer(self):
        self.projector.fail["power_state"] = NoReply("silence")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
        self.assert_film_played(projectionist)

    def test_a_PON_that_is_refused(self):
        """More often means the projector is already on, or mid-cool-down,
        than that the request was wrong."""
        self.projector.fail["power_on"] = Refused("ER401")
        self.projector.power = "standby"
        with settings(PROJECTOR_READY_SECONDS=5.0):
            projectionist = self.build()
            self.run_prepare(projectionist)
        self.assert_film_played(projectionist)

    def test_an_input_the_projector_does_not_have(self):
        self.projector.fail["set_input"] = Refused("ER401")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
        self.assert_film_played(projectionist)

    def test_a_lamp_that_never_lights(self):
        self.projector.power = "standby"
        # power_on() would set it to "on"; make it stay dark.
        self.projector.power_on = lambda: self.projector.calls.append("power_on")
        with settings(PROJECTOR_READY_SECONDS=30.0, PROJECTOR_WARMUP_SECONDS=10.0):
            projectionist = self.build()
            self.run_prepare(projectionist)
        self.assert_film_played(projectionist)
        # It gave up rather than waiting forever, and gave up at the deadline
        # rather than immediately.
        self.assertGreaterEqual(self.clock.slept, 30.0)

    def test_the_fault_is_recorded_for_the_page(self):
        self.projector.fail["power_state"] = Unreachable("no such device")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
        status = projectionist.projector_status()
        self.assertEqual(status["fault"], module.FAULT_TEXT)
        self.assertEqual(status["power"], "unknown")

    def test_the_same_fault_is_logged_only_once(self):
        """A projector left unplugged overnight would otherwise write four
        thousand identical lines onto an eMMC shared with everything else."""
        self.projector.fail["power_state"] = Unreachable("no such device")
        with settings(), mock.patch.object(module, "log") as log:
            projectionist = self.build()
            for _ in range(5):
                projectionist._observe_power()
        faults = [c for c in log.call_args_list if "projector: %s" in c.args]
        self.assertEqual(len(faults), 1)


class Refusals(Base):
    def test_a_second_film_while_one_is_being_prepared(self):
        with settings():
            projectionist = self.build()
            with mock.patch.object(projectionist, "_prepare"):
                projectionist.begin(FILM)
                with self.assertRaises(Busy):
                    projectionist.begin(OTHER)

    def test_a_film_while_one_is_playing(self):
        self.player.playing = "playing"
        with settings():
            with self.assertRaises(Busy):
                self.build().begin(FILM)

    def test_a_film_while_somebody_is_mirroring(self):
        """Refused immediately rather than after a warm-up. Reaching a lit lamp
        and only then being told somebody else has the projector wastes a
        minute of a child's patience and a minute of lamp life."""
        with settings(), mock.patch.multiple(
                module, airplay_active=lambda: True,
                airplay_confirmed=lambda: True):
            projectionist = self.build()
            with self.assertRaises(Busy):
                projectionist.begin(FILM)
        self.assertEqual(self.projector.calls, [],
                         "the projector should not be touched at all")

    def test_the_player_refusing_becomes_a_notice(self):
        """The POST answered 200 long before this happened, so the status poll
        is the only route the reason has back to the phone."""
        self.player.start_error = Busy("A film is already playing.")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
        self.assertEqual(projectionist.state(), "idle")
        self.assertEqual(projectionist.notice(), "A film is already playing.")

    def test_a_notice_does_not_outlive_what_it_explains(self):
        self.player.start_error = Busy("A film is already playing.")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
            self.assertTrue(projectionist.notice())
            self.clock.advance(module.NOTICE_SECONDS + 1)
            self.assertEqual(projectionist.notice(), "")

    def test_a_new_attempt_supersedes_the_last_explanation(self):
        self.player.start_error = Busy("A film is already playing.")
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
            self.player.start_error = None
            self.run_prepare(projectionist, OTHER)
            self.assertEqual(projectionist.notice(), "")


class Cancelling(Base):
    def test_never_mind_during_the_warm_up_starts_no_film(self):
        self.projector.power = "standby"
        self.projector.power_on = lambda: self.projector.calls.append("power_on")
        projectionist = None
        stopped = threading.Event()

        def sleep(seconds):
            # Cancel from inside the wait, which is where a child's thumb
            # actually lands: the sequence is asleep on the lamp blackout.
            self.clock.sleep(seconds)
            if not stopped.is_set():
                stopped.set()
                projectionist.stop()

        with settings(PROJECTOR_WARMUP_SECONDS=10.0):
            projectionist = Projectionist(self.projector, self.player,
                                          clock=self.clock, sleep=sleep)
            self.run_prepare(projectionist)

        self.assertEqual([c for c in self.player.calls if c[0] == "start"], [])
        self.assertEqual(projectionist.state(), "idle")
        self.assertEqual(projectionist.progress(), None)

    def test_cancelling_leaves_the_projector_alone(self):
        """A child who changes their mind and picks a different film should not
        have to sit through a second warm-up, and the keeper will switch it off
        in half an hour if nobody does."""
        self.projector.power = "standby"
        self.projector.power_on = lambda: self.projector.calls.append("power_on")
        projectionist = None
        stopped = threading.Event()

        def sleep(seconds):
            self.clock.sleep(seconds)
            if not stopped.is_set():
                stopped.set()
                projectionist.stop()

        with settings(PROJECTOR_WARMUP_SECONDS=10.0):
            projectionist = Projectionist(self.projector, self.player,
                                          clock=self.clock, sleep=sleep)
            self.run_prepare(projectionist)

        self.assertNotIn("power_off", self.projector.calls)

    def test_stop_reaches_the_player_when_a_film_is_running(self):
        with settings():
            projectionist = self.build()
            self.run_prepare(projectionist)
            projectionist.stop()
        self.assertIn(("stop",), self.player.calls)


class Progress(Base):
    def test_progress_is_none_when_nothing_is_being_prepared(self):
        self.assertIsNone(self.build().progress())

    def test_progress_names_the_step_and_its_words(self):
        with settings():
            projectionist = self.build()
            with mock.patch.object(projectionist, "_prepare"):
                projectionist.begin(FILM)
            reported = projectionist.progress()
        self.assertEqual(reported["step"], "waking")
        self.assertEqual(reported["label"], module.LABELS["waking"])
        self.assertEqual(reported["since"], 0.0)

    def test_the_film_names_itself_before_mpv_exists(self):
        """The page needs the id to draw a poster on the preparing view, and
        there is no mpv yet to ask."""
        with settings():
            projectionist = self.build()
            with mock.patch.object(projectionist, "_prepare"):
                projectionist.begin(FILM)
            self.assertEqual(projectionist.current_item(), FILM)
            self.assertEqual(projectionist.current_title(), "Ponyo")
            self.assertEqual(projectionist.state(), "preparing")


class Keeper(Base):
    """The thirty minutes, and the two AirPlay questions."""

    def test_it_switches_the_lamp_off_after_the_idle_period(self):
        self.projector.power = "on"
        with settings(PROJECTOR_IDLE_SECONDS=1800):
            projectionist = self.build()
            projectionist.tick()
            self.assertNotIn("power_off", self.projector.calls)
            self.clock.advance(1800)
            projectionist.tick()
        self.assertIn("power_off", self.projector.calls)

    def test_a_film_holds_the_lamp_on(self):
        self.projector.power = "on"
        self.player.playing = "playing"
        with settings(PROJECTOR_IDLE_SECONDS=1800):
            projectionist = self.build()
            for _ in range(5):
                self.clock.advance(1800)
                projectionist.tick()
        self.assertNotIn("power_off", self.projector.calls)

    def test_a_preparation_holds_the_lamp_on(self):
        """A cold lamp takes longer than some idle settings would allow, and
        switching it off halfway through lighting it would be absurd."""
        self.projector.power = "on"
        with settings(PROJECTOR_IDLE_SECONDS=30):
            projectionist = self.build()
            with mock.patch.object(projectionist, "_prepare"):
                projectionist.begin(FILM)
            self.clock.advance(300)
            projectionist.tick()
        self.assertNotIn("power_off", self.projector.calls)

    def test_mirroring_holds_the_lamp_on(self):
        self.projector.power = "on"
        with settings(PROJECTOR_IDLE_SECONDS=1800), mock.patch.multiple(
                module, airplay_active=lambda: True,
                airplay_confirmed=lambda: True):
            projectionist = self.build()
            self.clock.advance(1800)
            projectionist.tick()
        self.assertNotIn("power_off", self.projector.calls)

    def test_a_page_left_open_does_not(self):
        """Nothing in tick() consults the HTTP layer, and that is the whole
        decision: a phone polling /api/status every three seconds in somebody's
        pocket must not keep a lamp burning all night."""
        self.projector.power = "on"
        with settings(PROJECTOR_IDLE_SECONDS=1800):
            projectionist = self.build()
            self.clock.advance(1800)
            projectionist.tick()
        self.assertIn("power_off", self.projector.calls)

    def test_zero_minutes_disables_it(self):
        self.projector.power = "on"
        with settings(PROJECTOR_IDLE_SECONDS=0):
            projectionist = self.build()
            self.clock.advance(86400)
            projectionist.tick()
        self.assertNotIn("power_off", self.projector.calls)

    def test_it_does_not_keep_sending_POF_to_a_projector_already_off(self):
        self.projector.power = "standby"
        with settings(PROJECTOR_IDLE_SECONDS=1800):
            projectionist = self.build()
            self.clock.advance(1800)
            projectionist.tick()
            projectionist.tick()
        self.assertNotIn("power_off", self.projector.calls)


class AirplayWake(Base):
    """Striking a lamp is the expensive direction, so it asks harder."""

    def wake_setting(self, **extra):
        extra.setdefault("PROJECTOR_WAKE_ON_AIRPLAY", True)
        extra.setdefault("PROJECTOR_AIRPLAY_WAKE_TICKS", 2)
        return settings(**extra)

    def test_a_sustained_session_wakes_it(self):
        self.projector.power = "standby"
        with self.wake_setting(), mock.patch.multiple(
                module, airplay_active=lambda: True,
                airplay_confirmed=lambda: True):
            projectionist = self.build()
            projectionist.tick()
            self.assertNotIn("power_on", self.projector.calls,
                             "one tick is not sustained")
            projectionist.tick()
        self.assertIn("power_on", self.projector.calls)

    def test_a_glance_at_the_picker_does_not(self):
        """iOS opens short-lived connections to UxPlay's port merely from
        having the AirPlay picker on screen. The cheap sample sees them; the
        debounced one does not, and only the debounced one may strike a lamp."""
        self.projector.power = "standby"
        with self.wake_setting(), mock.patch.multiple(
                module, airplay_active=lambda: True,
                airplay_confirmed=lambda: False):
            projectionist = self.build()
            for _ in range(10):
                projectionist.tick()
        self.assertNotIn("power_on", self.projector.calls)

    def test_an_interrupted_session_starts_the_count_again(self):
        self.projector.power = "standby"
        confirmed = [True, False, True]
        with self.wake_setting(PROJECTOR_AIRPLAY_WAKE_TICKS=2), \
                mock.patch.multiple(
                    module, airplay_active=lambda: True,
                    airplay_confirmed=lambda: confirmed.pop(0)):
            projectionist = self.build()
            for _ in range(3):
                projectionist.tick()
        self.assertNotIn("power_on", self.projector.calls)

    def test_the_input_is_selected_once_the_lamp_is_lit(self):
        """It cannot be selected at wake time: the projector is deaf for ten
        seconds after PON and will not accept IIS until the lamp is up."""
        self.projector.power = "standby"
        with self.wake_setting(PROJECTOR_AIRPLAY_WAKE_TICKS=1,
                               PROJECTOR_INPUT="HD3"), \
                mock.patch.multiple(module, airplay_active=lambda: True,
                                    airplay_confirmed=lambda: True):
            projectionist = self.build()
            projectionist.tick()
            self.assertNotIn("set_input:HD3", self.projector.calls)
            # The lamp comes up; the next tick points it at the stick.
            projectionist.tick()
        self.assertIn("set_input:HD3", self.projector.calls)

    def test_it_can_be_switched_off(self):
        self.projector.power = "standby"
        with self.wake_setting(PROJECTOR_WAKE_ON_AIRPLAY=False), \
                mock.patch.multiple(module, airplay_active=lambda: True,
                                    airplay_confirmed=lambda: True):
            projectionist = self.build()
            for _ in range(5):
                projectionist.tick()
        self.assertNotIn("power_on", self.projector.calls)

    def test_a_projector_already_on_is_not_woken(self):
        self.projector.power = "on"
        with self.wake_setting(), mock.patch.multiple(
                module, airplay_active=lambda: True,
                airplay_confirmed=lambda: True):
            projectionist = self.build()
            for _ in range(5):
                projectionist.tick()
        self.assertNotIn("power_on", self.projector.calls)


class Shutdown(Base):
    def test_close_releases_the_port(self):
        projectionist = self.build()
        projectionist.close()
        self.assertTrue(self.projector.closed)


class ByHand(Base):
    """The page's hidden menu: on and off because somebody asked."""

    def test_on_owes_the_input_and_restarts_the_idle_count(self):
        projectionist = self.build()
        self.clock.advance(3600)
        with settings():
            self.assertIn("Switching on", projectionist.power(True))
            self.assertEqual(self.projector.calls, ["power_on"])
            # Reported as on at once, so the menu offers "off" next.
            self.assertEqual(projectionist.projector_status()["power"], "on")
            # The next tick sees the lamp lit and selects the input...
            projectionist.tick()
        self.assertIn("set_input:HD3", self.projector.calls)
        # ...and the idle switch-off counts from the press, not from an hour ago.
        self.assertNotIn("power_off", self.projector.calls)

    def test_off_while_idle(self):
        self.projector.power = "on"
        projectionist = self.build()
        with settings():
            self.assertEqual(projectionist.power(False), "Switching off.")
        self.assertEqual(self.projector.calls, ["power_off"])
        self.assertEqual(projectionist.projector_status()["power"], "standby")

    def test_off_is_refused_mid_film(self):
        self.player.playing = "playing"
        projectionist = self.build()
        with settings(), self.assertRaises(Busy):
            projectionist.power(False)
        self.assertNotIn("power_off", self.projector.calls)

    def test_a_cooling_projector_refuses_and_says_so(self):
        self.projector.fail["power_on"] = Refused("ER401")
        projectionist = self.build()
        with settings(), self.assertRaises(Refused):
            projectionist.power(True)

    def test_no_projector_is_busy_not_a_crash(self):
        from playstick.projector import NullProjector
        projectionist = Projectionist(NullProjector(), self.player,
                                      clock=self.clock, sleep=self.clock.sleep)
        with self.assertRaises(Busy):
            projectionist.power(True)


class SettingsView(Base):
    """The settings behind the gear: read live, and only with the lamp lit."""

    def test_lit_lists_the_drivers_settings(self):
        self.projector.power = "on"
        answer = self.build().projector_settings()
        self.assertEqual(answer["power"], "on")
        self.assertEqual(answer["settings"][0]["value"], "CN1")

    def test_in_standby_the_list_is_empty_and_nothing_is_asked(self):
        """The projector echoes every inquiry in standby; asking would only
        produce six blanks and six exchanges."""
        answer = self.build().projector_settings()
        self.assertEqual(answer, {"power": "standby", "settings": []})
        self.assertNotIn("settings", self.projector.calls)

    def test_a_change_answers_with_what_the_projector_now_says(self):
        self.projector.power = "on"
        answer = self.build().change_setting("picture", "DYN")
        self.assertIn("change:picture:DYN", self.projector.calls)
        self.assertEqual(answer["settings"][0]["value"], "DYN")

    def test_a_change_in_standby_is_busy_and_never_sent(self):
        with self.assertRaises(Busy):
            self.build().change_setting("picture", "DYN")
        self.assertFalse([c for c in self.projector.calls
                          if c.startswith("change")])

    def test_a_refused_value_reaches_the_caller(self):
        self.projector.power = "on"
        self.projector.fail["change"] = Refused("did not take")
        with self.assertRaises(Refused):
            self.build().change_setting("picture", "VIV")

    def test_no_projector_is_busy(self):
        from playstick.projector import NullProjector
        projectionist = Projectionist(NullProjector(), self.player,
                                      clock=self.clock, sleep=self.clock.sleep)
        for call in (projectionist.projector_settings,
                     lambda: projectionist.change_setting("picture", "NOR")):
            with self.subTest(call=call), self.assertRaises(Busy):
                call()


class ScriptedNas:
    """NasWatcher's surface as the projectionist uses it, scripted.

    Writes into the projector's call list rather than one of its own, so the
    order of PON, the wake, the pings and the remount is one list to assert.
    `boot_pings` is how many pings go unanswered after the wake before the NAS
    answers; None means it never does.
    """

    configured = True

    def __init__(self, calls, up=False, boot_pings=0, wake_reply=(True, "")):
        self.calls = calls
        self.up = up
        self.boot_pings = boot_pings
        self.wake_reply = wake_reply
        self.woken = False
        self.on_ping = None

    def ping(self):
        self.calls.append("nas:ping")
        if self.on_ping:
            self.on_ping()
        if self.up:
            return True
        if self.woken and self.boot_pings is not None:
            if self.boot_pings <= 0:
                self.up = True
                return True
            self.boot_pings -= 1
        return False

    def wake(self):
        self.calls.append("nas:wake")
        if self.wake_reply[0]:
            self.woken = True
        return self.wake_reply

    def recover(self):
        self.calls.append("nas:recover")


class Share:
    """The share probe: answers from a script, the last one repeating."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    def __call__(self):
        self.asked += 1
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


class SleepingNas(Base):
    """A film picked off the grid while the NAS sleeps. The grid is the local
    database and is there regardless; the films are not, and this is the
    sequence that goes and gets them."""

    def build_with(self, nas, share):
        return self.build(nas=nas, share=share)

    def steps_of(self, projectionist):
        steps = []
        real = projectionist._step
        projectionist._step = lambda prep, name: (steps.append(name),
                                                  real(prep, name))[1]
        return steps

    def test_the_lamp_then_the_wake_then_ping_then_the_mount(self):
        nas = ScriptedNas(self.projector.calls, boot_pings=2)
        share = Share(False, None, True)
        with settings(PROJECTOR_WARMUP_SECONDS=10.0):
            projectionist = self.build_with(nas, share)
            steps = self.steps_of(projectionist)
            self.run_prepare(projectionist)

        calls = self.projector.calls
        self.assertEqual(calls[:4], ["power_state", "power_on", "nas:ping",
                                     "nas:wake"])
        # Pinged until it answered, and only then was the mount reset.
        self.assertEqual(calls[4:7], ["nas:ping"] * 3)
        self.assertEqual(calls[7], "nas:recover")
        self.assertEqual(steps, ["nas", "share", "warming", "input", "display",
                                 "starting"])
        # PON went out once, before the wait; the strike does not send another.
        self.assertEqual(calls.count("power_on"), 1)
        self.assertEqual(self.player.calls, [("start", FILM["id"])])

    def test_the_blackout_is_counted_from_the_early_pon(self):
        """Twenty seconds waiting on the NAS is twenty seconds of lamp warm-up
        already done, and the ten-second blackout is not spent again."""
        nas = ScriptedNas(self.projector.calls, boot_pings=10)
        with settings(PROJECTOR_WARMUP_SECONDS=10.0):
            self.run_prepare(self.build_with(nas, Share(False, False, True)))
        # Ten unanswered pings two seconds apart, one look at a mount not yet
        # up, and no blackout on top.
        self.assertEqual(self.clock.slept, 22.0)
        self.assertEqual(self.player.calls, [("start", FILM["id"])])

    def test_a_short_wait_still_sits_out_the_rest_of_the_blackout(self):
        nas = ScriptedNas(self.projector.calls, boot_pings=1)
        with settings(PROJECTOR_WARMUP_SECONDS=10.0):
            self.run_prepare(self.build_with(nas, Share(False, True)))
        self.assertEqual(self.clock.slept, 10.0)

    def test_a_nas_that_never_wakes_ends_with_a_sentence(self):
        nas = ScriptedNas(self.projector.calls, boot_pings=None)
        with settings(NAS_WAKE_WINDOW=30.0):
            projectionist = self.build_with(nas, Share(False))
            self.run_prepare(projectionist)
            self.assertEqual(projectionist.notice(), module.NAS_ASLEEP_TEXT)
        self.assertEqual(self.player.calls, [])
        self.assertNotIn("nas:recover", self.projector.calls)
        self.assertEqual(projectionist.state(), "idle")

    def test_a_nas_that_wakes_but_whose_share_never_comes(self):
        nas = ScriptedNas(self.projector.calls, boot_pings=0)
        with settings(SHARE_MOUNT_SECONDS=10.0):
            projectionist = self.build_with(nas, Share(False))
            self.run_prepare(projectionist)
            self.assertEqual(projectionist.notice(), module.SHARE_MISSING_TEXT)
        self.assertEqual(self.player.calls, [])

    def test_a_nas_already_answering_ping_is_not_sent_a_packet(self):
        """Up, with the mount stale or failed: the remount is what is owed."""
        nas = ScriptedNas(self.projector.calls, up=True)
        with settings():
            self.run_prepare(self.build_with(nas, Share(False, True)))
        self.assertNotIn("nas:wake", self.projector.calls)
        self.assertIn("nas:recover", self.projector.calls)
        self.assertEqual(self.player.calls, [("start", FILM["id"])])

    def test_a_refused_wake_still_waits_for_somebodys_finger(self):
        """No MAC, or a cooldown: no packet from here, but the NAS's own button
        is still a way for this to work, so the wait goes on."""
        nas = ScriptedNas(self.projector.calls,
                          wake_reply=(False, "Already waking it up."))
        pings = []

        def press_the_button():
            pings.append(1)
            if len(pings) == 3:
                nas.up = True
        nas.on_ping = press_the_button
        with settings():
            self.run_prepare(self.build_with(nas, Share(False, True)))
        self.assertEqual(self.player.calls, [("start", FILM["id"])])

    def test_stop_during_the_wait_abandons_it(self):
        nas = ScriptedNas(self.projector.calls, boot_pings=None)
        with settings():
            projectionist = self.build_with(nas, Share(False))
            nas.on_ping = lambda: (len(self.projector.calls) > 6
                                   and projectionist.stop())
            self.run_prepare(projectionist)
            self.assertEqual(projectionist.notice(), "")
        self.assertNotIn(("start", FILM["id"]), self.player.calls)
        self.assertEqual(projectionist.state(), "idle")

    def test_a_share_that_is_there_leaves_the_nas_alone(self):
        nas = ScriptedNas(self.projector.calls, up=True)
        share = Share(True)
        with settings():
            projectionist = self.build_with(nas, share)
            steps = self.steps_of(projectionist)
            self.run_prepare(projectionist)
        self.assertEqual(share.asked, 1)
        self.assertFalse([c for c in self.projector.calls if c.startswith("nas:")])
        self.assertEqual(steps, ["warming", "input", "display", "starting"])

    def test_calibration_does_not_wait_for_the_films(self):
        """The wizard draws on a black screen and reads nothing off the share,
        so a sleeping NAS must not stand between a phone and it."""
        nas = ScriptedNas(self.projector.calls, up=False)
        share = Share(False)
        with settings():
            projectionist = self.build_with(nas, share)
            steps = self.steps_of(projectionist)
            self.run_prepare(projectionist, item={"id": "calibrate",
                                                  "kind": "calibrate",
                                                  "title": "Calibration"})
        self.assertEqual(share.asked, 0)
        self.assertFalse([c for c in self.projector.calls if c.startswith("nas:")])
        self.assertNotIn("share", steps)
        self.assertEqual(self.player.calls, [("start", "calibrate")])

    def test_no_nas_configured_gives_the_automount_a_moment_then_says_so(self):
        with settings(SHARE_UNMANAGED_SECONDS=6.0):
            projectionist = self.build_with(None, Share(False))
            self.run_prepare(projectionist)
            self.assertEqual(projectionist.notice(), module.SHARE_MISSING_TEXT)
        self.assertEqual(self.clock.slept, 6.0)
        self.assertEqual(self.player.calls, [])


if __name__ == "__main__":
    unittest.main()
