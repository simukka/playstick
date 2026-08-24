"""Everything between a tap on a poster and a film on the screen, and the
decision to switch the lamp off again afterwards.

WHY THIS IS ONE OBJECT AND NOT TWO

Starting a film and turning the projector off look like separate jobs, and they
are not: both are answers to "should the lamp be lit right now", both reach for
the same serial port, and both need the same notion of whether anything is
happening. Splitting them would mean two objects sharing a lock, a projector
and an idea of activity -- which is one object with a seam drawn through it.

WHY THE WORK HAPPENS ON A THREAD

Before this, POST /api/play blocked until mpv was running, which took as long
as stopping the AirPlay receiver -- twenty seconds at worst, usually two. A
lamp changes that arithmetic: a cold PT-AE4000 takes the better part of a
minute to answer QPW with 001, and a request that hangs for a minute is a
request every phone browser gives up on. So begin() returns at once and the
page watches /api/status, which is the same mechanism it already uses for
everything else.

That is also what makes the feature visible. A child who taps a poster and
sees nothing for forty seconds has no way to tell a warming lamp from a broken
appliance, and the second guess is the one they act on. Each step below names
itself so the page can say which of the two it is.

WHAT HAPPENS WHEN THE PROJECTOR DOES NOT ANSWER

The film plays anyway. Every ProjectorError in the sequence is logged, recorded
for the page to mention, and stepped over -- the same judgement library.py
makes about a corrupt index. If the lamp will not strike, the most likely
explanations are that somebody already switched the projector on with the
remote, or that a cable is loose; in the first case the film is exactly what
was wanted, and in the second an adult standing in the room can fix in two
seconds something this daemon cannot fix at all. Refusing to play would help
nobody.

mpv failing is different, and keeps the behaviour it always had: the attempt
ends, the message reaches the page, and the grid comes back.
"""

import threading
import time

from .airplay import airplay_active, airplay_confirmed
from .config import (
    PROJECTOR_AIRPLAY_WAKE_TICKS, PROJECTOR_IDLE_SECONDS, PROJECTOR_INPUT,
    PROJECTOR_READY_SECONDS, PROJECTOR_TICK_SECONDS, PROJECTOR_WAKE_ON_AIRPLAY,
    PROJECTOR_WARMUP_SECONDS, log,
)
from .player import Busy
from .projector import ON, STANDBY, UNKNOWN, ProjectorError


# The steps, in order, with the words the page shows verbatim. They are written
# for the person watching rather than for the person debugging -- "Waiting for
# the lamp" is what is happening; "polling QPW until 001" is in the journal.
STEPS = (
    ("waking", "Waking the projector up…"),
    ("warming", "Waiting for the lamp…"),
    ("input", "Pointing it at the movie…"),
    ("display", "Making room on the screen…"),
    ("starting", "Starting the movie…"),
)
LABELS = dict(STEPS)

# Shown on the page when the projector cannot be reached. One sentence, no
# diagnosis: the detail goes to the journal, where somebody can act on it.
FAULT_TEXT = "I couldn't reach the projector."

# How long to wait between PON attempts while the lamp refuses to light. The
# case this covers is a projector still running its post-POF cool-down, during
# which PON is ignored outright; the fans take a minute or so and there is no
# way to ask how much longer.
RESTRIKE_INTERVAL = 20.0

# Granularity of every interruptible wait. Small enough that "Never mind"
# during a lamp warm-up feels immediate, large enough not to spin.
NAP = 0.25

# How long a failed preparation keeps saying so on /api/status.
#
# It has to be a window rather than a message consumed by the first reader:
# several phones may have the page open, each polling on its own schedule, and
# a notice delivered once would reach exactly one of them -- most likely not
# the one belonging to the child who pressed the poster. Fifteen seconds is
# comfortably longer than the three-second grid poll and short enough that a
# stale explanation never outlives the thing it explains.
NOTICE_SECONDS = 15.0


class Projectionist:
    """The facade the HTTP layer asks about state, and the thread that acts."""

    def __init__(self, projector, player, clock=time.monotonic, sleep=time.sleep):
        self._projector = projector
        self._player = player
        # Injected so the tests can drive a thirty-minute idle timeout by
        # advancing a variable instead of waiting. The same technique
        # tests/js/clock.js already uses on the headphone sync controller.
        self._clock = clock
        self._sleep = sleep

        self._lock = threading.RLock()
        self._preparing = None          # the dict below, or None
        self._fault = ""                # "" or FAULT_TEXT
        self._power = UNKNOWN           # last answer QPW gave
        self._last_activity = clock()
        self._airplay_streak = 0        # consecutive ticks of confirmed mirroring
        self._owe_input = False         # woken for AirPlay; select the input once lit
        self._logged_fault = ""
        self._notice = ("", 0.0)        # why the last attempt gave up, and when

    # -- what the HTTP layer asks

    def state(self):
        """"preparing" while getting ready, otherwise whatever the player says.

        This is the composite the rest of the daemon reads, which is why
        Thumbs is constructed with a Projectionist rather than a Player: its
        run loop already blocks while state() is not "idle", so passing this
        object is the whole of what stops it extracting posters over CIFS while
        a lamp is warming up.
        """
        with self._lock:
            if self._preparing is not None:
                return "preparing"
        return self._player.state()

    def current_item(self):
        with self._lock:
            if self._preparing is not None:
                return self._preparing["item"]
        return self._player.current_item()

    def current_title(self):
        item = self.current_item()
        return item["title"] if item else ""

    def progress(self):
        """The step the page draws, or None when nothing is being prepared."""
        with self._lock:
            prep = self._preparing
            if prep is None:
                return None
            return {
                "step": prep["step"],
                "label": LABELS.get(prep["step"], ""),
                "since": round(self._clock() - prep["at"], 1),
            }

    def projector_status(self):
        with self._lock:
            return {"model": self._projector.model,
                    "power": self._power,
                    "fault": self._fault}

    def notice(self):
        """Why the last attempt gave up, for a few seconds afterwards, or "".

        A preparation that fails does so on a thread, long after the POST that
        started it returned 200. Without this the page would watch the state
        go back to "idle", return to the grid, and never say why -- which from
        a child's side of it is a poster that does nothing when you press it.
        """
        with self._lock:
            message, at = self._notice
        if not message or self._clock() - at > NOTICE_SECONDS:
            return ""
        return message

    # -- starting a film

    def begin(self, item):
        """Accept a film and return; the work happens on a thread.

        The AirPlay check is here rather than only in Player.start so that a
        refusal is immediate. Getting as far as a warm lamp and then being told
        somebody else is using the projector wastes a minute of a child's
        patience and a minute of lamp life, and airplay_confirmed() costs
        nothing at all in the ordinary case where nobody is mirroring -- it
        asks ss once, gets nothing, and returns.
        """
        with self._lock:
            if self._preparing is not None:
                raise Busy("A movie is already starting.")
            if self._player.state() != "idle":
                raise Busy("A film is already playing.")
            if airplay_confirmed():
                raise Busy("The projector is being used for AirPlay.")
            prep = {
                "item": item,
                "step": STEPS[0][0],
                "at": self._clock(),
                "cancel": threading.Event(),
                # Set when the thread has finished, whatever the outcome.
                # Nothing in the daemon waits on it -- the page learns from
                # /api/status -- but a test that did not have it would have to
                # decide how long to sleep before asserting, which is how a
                # suite acquires the failures that only happen on a busy
                # machine.
                "done": threading.Event(),
            }
            self._preparing = prep
            self._last_activity = self._clock()
            # A new attempt supersedes the last one's explanation, which would
            # otherwise sit on the page for its remaining seconds while the
            # thing it described is visibly being retried.
            self._notice = ("", 0.0)
        threading.Thread(target=self._prepare, args=(prep,), daemon=True).start()
        return prep

    def stop(self):
        """Abandon a preparation, stop a film, or both.

        One method rather than two because the page has one STOP button and
        should not have to know which of the two states it is in. The
        projector is deliberately left alone: a child who changes their mind
        and picks a different film should not have to sit through a second
        warm-up, and the keeper below will switch it off in half an hour if
        nobody does.
        """
        with self._lock:
            prep = self._preparing
        if prep is not None:
            prep["cancel"].set()
        self._player.stop()

    # -- the sequence

    def _prepare(self, prep):
        item = prep["item"]
        try:
            power = self._observe_power()

            # UNKNOWN means "no projector, or one that will not say", and both
            # of those must skip the wait rather than sit through it. This is
            # the line that keeps an appliance with no serial cable behaving
            # exactly as it did before this feature existed.
            if power == STANDBY:
                if not self._strike(prep):
                    return self._abandon(prep, "")

            if not self._still_wanted(prep):
                return self._abandon(prep, "")

            if PROJECTOR_INPUT:
                self._step(prep, "input")
                self._select_input()

            if not self._still_wanted(prep):
                return self._abandon(prep, "")

            self._player.start(item, progress=lambda name: self._step(prep, name))
        except Busy as exc:
            log("could not start %s: %s", item.get("title", "?"), exc)
            self._abandon(prep, str(exc))
        except Exception as exc:                         # noqa: BLE001
            log("preparing %s failed: %s", item.get("title", "?"), exc)
            self._abandon(prep, "The movie would not start.")
        else:
            self._finish(prep)
        finally:
            prep["done"].set()

    def _strike(self, prep):
        """Light the lamp and wait for it to say so. False if cancelled.

        Returning True on a projector that never lights is not an oversight.
        Past the deadline the sequence carries on and starts the film, for the
        reason in the module header -- the screen may well already be on.
        """
        self._step(prep, "warming")
        try:
            self._projector.power_on()
        except ProjectorError as exc:
            # Worth continuing rather than giving up: PON is the one command
            # standby is documented to accept, so a refusal here more often
            # means the projector is already on, or mid-cool-down, than that
            # the request was wrong.
            self._note(exc, "PON")

        # The documented blackout. Polling inside it produces timeouts that
        # say nothing about the cable, so the wait is spent rather than the
        # port being asked pointless questions.
        if not self._nap(prep, PROJECTOR_WARMUP_SECONDS):
            return False

        deadline = self._clock() + PROJECTOR_READY_SECONDS
        last_attempt = self._clock()
        while self._clock() < deadline:
            if self._observe_power() == ON:
                return True
            if not self._nap(prep, 1.0):
                return False
            # A projector still cooling down ignored the first PON outright.
            # There is no way to ask how much longer it needs, so ask again.
            if self._clock() - last_attempt >= RESTRIKE_INTERVAL:
                last_attempt = self._clock()
                log("the lamp is not lit yet; sending PON again")
                try:
                    self._projector.power_on()
                except ProjectorError as exc:
                    self._note(exc, "PON")

        log("gave up waiting for the lamp after %.0fs; starting the film anyway",
            PROJECTOR_READY_SECONDS)
        return True

    def _select_input(self):
        try:
            self._projector.set_input(PROJECTOR_INPUT)
        except ProjectorError as exc:
            self._note(exc, "IIS:%s" % PROJECTOR_INPUT)
            return
        # Best effort, and never fatal: the echo already said the command was
        # accepted, and this only catches a projector that accepted it and did
        # something else. Worth one line in the journal, worth nothing to the
        # child watching.
        landed = self._projector.current_input()
        if landed and landed != PROJECTOR_INPUT:
            log("asked for input %s; the projector says it is on %s",
                PROJECTOR_INPUT, landed)

    # -- bookkeeping for the sequence

    def _step(self, prep, name):
        with self._lock:
            if self._preparing is prep:
                prep["step"] = name
        log("preparing: %s", LABELS.get(name, name))

    def _still_wanted(self, prep):
        return not prep["cancel"].is_set()

    def _nap(self, prep, seconds):
        """Sleep, in pieces, watching for a cancel. False if one arrived."""
        end = self._clock() + seconds
        while True:
            if prep["cancel"].is_set():
                return False
            left = end - self._clock()
            if left <= 0:
                return True
            self._sleep(min(NAP, left))

    def _clear(self, prep):
        with self._lock:
            if self._preparing is prep:
                self._preparing = None
                self._last_activity = self._clock()

    def _finish(self, prep):
        self._clear(prep)

    def _abandon(self, prep, message):
        """Give up on a preparation. An empty message means it was cancelled.

        The film is stopped rather than assumed not to have started: the last
        step of the sequence is Player.start, and a cancel that lands during it
        would otherwise leave mpv running with nothing tracking it.
        """
        self._clear(prep)
        if message:
            with self._lock:
                self._notice = (message, self._clock())
            log("gave up preparing %s: %s", prep["item"].get("title", "?"), message)
        else:
            log("preparing %s was cancelled", prep["item"].get("title", "?"))
            self._player.stop()

    # -- the projector, with the faults recorded rather than raised

    def _observe_power(self):
        try:
            power = self._projector.power_state()
        except ProjectorError as exc:
            self._note(exc, "QPW")
            with self._lock:
                self._power = UNKNOWN
            return UNKNOWN
        with self._lock:
            self._power = power
            self._fault = ""
            self._logged_fault = ""
        return power

    def _note(self, exc, what):
        """Record a fault for the page and log it once.

        Once, because the keeper asks the same question every fifteen seconds
        and a projector left unplugged overnight would otherwise write four
        thousand identical lines onto an eMMC this appliance shares with
        everything else.
        """
        detail = "%s: %s" % (what, exc)
        with self._lock:
            self._fault = FAULT_TEXT
            first = detail != self._logged_fault
            self._logged_fault = detail
        if first:
            log("projector: %s", detail)

    # -- the lamp keeper

    def run(self, stopping):
        """The thread main(). One tick, then wait, until shutdown."""
        while not stopping.is_set():
            try:
                self.tick()
            except Exception as exc:                     # noqa: BLE001
                log("projector keeper: %s", exc)
            stopping.wait(PROJECTOR_TICK_SECONDS)

    def tick(self):
        """One pass of the power policy. The unit the tests drive.

        THE TWO AIRPLAY QUESTIONS ARE DELIBERATELY DIFFERENT, and it is the
        same asymmetry airplay.py already draws, for a stronger reason. Keeping
        the lamp lit uses the cheap single sample: a false positive there only
        postpones a power-off, which costs nothing. Striking a cold lamp
        because somebody opened the AirPlay picker across the room and closed
        it again costs a warm-up, some lamp life, and a projector switching
        itself on in an empty room -- so that direction wants the debounced
        check, sustained across several ticks.
        """
        now = self._clock()
        playing = self.state() != "idle"
        mirroring = airplay_active()
        if playing or mirroring:
            self._last_activity = now

        power = self._observe_power()

        # Woken for a mirroring session and now lit: point it at the stick.
        if self._owe_input and power == ON:
            self._owe_input = False
            if PROJECTOR_INPUT:
                self._select_input()

        if mirroring and not playing:
            self._consider_wake(power)
        else:
            self._airplay_streak = 0

        if playing or mirroring:
            return
        if power == ON and PROJECTOR_IDLE_SECONDS \
                and now - self._last_activity >= PROJECTOR_IDLE_SECONDS:
            # %g rather than integer minutes: the setting is in minutes, but
            # it is a float and a developer testing with 0.05 of one should
            # not read "nothing for 0 minutes" in the journal.
            log("nothing for %g minutes; switching the projector off",
                PROJECTOR_IDLE_SECONDS / 60.0)
            try:
                self._projector.power_off()
            except ProjectorError as exc:
                self._note(exc, "POF")
            else:
                with self._lock:
                    self._power = STANDBY
                    self._last_activity = now

    def _consider_wake(self, power):
        if not PROJECTOR_WAKE_ON_AIRPLAY or power != STANDBY:
            self._airplay_streak = 0
            return
        # The expensive check, and only here: reached solely when the cheap one
        # has already said something is connected and the projector is off.
        if not airplay_confirmed():
            self._airplay_streak = 0
            return
        self._airplay_streak += 1
        if self._airplay_streak < PROJECTOR_AIRPLAY_WAKE_TICKS:
            return
        self._airplay_streak = 0
        log("a mirroring session has held for %d ticks; waking the projector",
            PROJECTOR_AIRPLAY_WAKE_TICKS)
        try:
            self._projector.power_on()
        except ProjectorError as exc:
            self._note(exc, "PON")
            return
        # The input cannot be selected yet -- the projector is deaf for the
        # next ten seconds and will not accept IIS until the lamp is up. A
        # later tick does it, once QPW says the lamp is lit.
        self._owe_input = True

    def close(self):
        self._projector.close()
