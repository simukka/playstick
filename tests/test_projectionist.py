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


if __name__ == "__main__":
    unittest.main()
