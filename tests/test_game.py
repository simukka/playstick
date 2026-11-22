"""The game runner, end to end against the real frontend and a stub core.

tests/fixtures/stub_core.c is a libretro core that emulates nothing. It is
compiled here with the system C compiler, and the real playstick-game.py runs
it with --headless, so everything between a phone's button and the core is the
shipped code: the runner's seats, the control socket, the frontend's input
callback and its sound resampler. The phones are fakes with the same methods
as a ws.Connection.

The core copies the joypad bits it was given into its save RAM, and the
frontend writes that out when the game stops. So the .srm file in the saves
directory is how a test sees which buttons reached the core.
"""

import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import TMP, LIBRARY_DIR                       # noqa: E402
from playstick import config, game, ws                      # noqa: E402
from playstick.player import Busy                           # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(HERE, "..", "src", "server", "playstick-game.py")
CC = shutil.which("cc") or shutil.which("gcc")
WORK = os.path.join(TMP, "game")
CORE = os.path.join(WORK, "stub_core.so")


def setUpModule():
    if not CC:
        raise unittest.SkipTest("no C compiler to build the stub core")
    os.makedirs(WORK, exist_ok=True)
    subprocess.check_call([CC, "-shared", "-fPIC", "-O1", "-o", CORE,
                           os.path.join(HERE, "fixtures", "stub_core.c")])


class FakePhone:
    """What GameRunner.serve needs from a ws.Connection."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.texts = []
        self.audio = []
        self.closed = False
        self.got_text = threading.Event()

    # The runner's side.
    def send_text(self, obj):
        self.texts.append(obj)
        self.got_text.set()

    def send_binary(self, data):
        self.audio.append(data)

    def recv(self):
        item = self.inbox.get(timeout=10)
        if item is None:
            raise ws.Closed("gone")
        return item

    def settimeout(self, _seconds):
        pass

    def close(self, _code=1000):
        self.closed = True

    def drain(self, timeout=1.0):
        pass

    # The test's side.
    def join(self, how="auto"):
        self.inbox.put((ws.TEXT, ('{"join": "%s"}' % how).encode()))

    def press(self, mask, seq=0):
        self.inbox.put((ws.BINARY, struct.pack("!BH", seq, mask)))

    def hang_up(self):
        self.inbox.put(None)

    @property
    def role(self):
        return self.texts[-1]["role"] if self.texts else None


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class GameTest(unittest.TestCase):
    def setUp(self):
        self.saves = os.path.join(WORK, "saves-%s" % self._testMethodName)
        rom = os.path.join(LIBRARY_DIR, "roms", "stub.nes")
        os.makedirs(os.path.dirname(rom), exist_ok=True)
        with open(rom, "wb") as handle:
            handle.write(b"NES\x1a" + b"\x01" * 60)
        self.item = {"id": "00000000000000aa", "title": "Stub", "path": rom,
                     "kind": "game", "system": "nes", "players": 2}
        patches = [
            mock.patch.object(config, "GAME_PYTHON", sys.executable),
            mock.patch.object(config, "GAME_FRONTEND", FRONTEND),
            mock.patch.object(config, "GAME_SOCKET",
                              os.path.join(WORK, "game.sock")),
            mock.patch.object(config, "GAME_SAVES", self.saves),
            mock.patch.object(config, "GAME_EXTRA_ARGS", ["--headless"]),
            mock.patch.object(config, "GAME_CORES", {"nes": CORE}),
            mock.patch.object(config, "GAMES_ENABLED", True),
            mock.patch.object(config, "LIBRARY", LIBRARY_DIR),
            mock.patch.object(game.stage, "take_display", return_value=False),
            mock.patch.object(game.stage, "give_back_display"),
            mock.patch.object(game.stage, "open_tty", return_value=None),
            mock.patch.object(game, "airplay_confirmed", return_value=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.runner = game.GameRunner()
        self.addCleanup(self.runner.stop)

    def connect(self):
        phone = FakePhone()
        thread = threading.Thread(target=self.runner.serve, args=(phone,),
                                  daemon=True)
        thread.start()
        self.assertTrue(phone.got_text.wait(5))
        self.addCleanup(phone.hang_up)
        return phone

    def sram(self):
        with open(os.path.join(self.saves, os.listdir(self.saves)[0]),
                  "rb") as handle:
            return handle.read()


class StartingAndStopping(GameTest):
    def test_a_game_starts_runs_and_stops(self):
        self.runner.start(self.item)
        self.assertTrue(self.runner.running())
        self.assertEqual(self.runner.current_item()["id"], self.item["id"])
        self.assertEqual(self.runner.status()["system"], "nes")
        self.runner.stop()
        self.assertFalse(self.runner.running())
        game.stage.give_back_display.assert_called_once_with(False)

    def test_a_system_with_no_core_is_refused_with_a_sentence(self):
        with self.assertRaises(Busy) as caught:
            self.runner.start(dict(self.item, system="gb"))
        self.assertIn("gb", str(caught.exception))
        game.stage.take_display.assert_not_called()

    def test_a_rom_outside_the_library_is_refused(self):
        with self.assertRaises(Busy):
            self.runner.start(dict(self.item, path="/etc/hostname"))

    def test_a_cartridge_the_core_refuses_is_reported_not_hung(self):
        """The stub core refuses an empty file. The frontend says why on the
        control socket, and start() raises it rather than waiting out its
        timeout."""
        empty = os.path.join(LIBRARY_DIR, "roms", "empty.nes")
        open(empty, "wb").close()
        started = time.monotonic()
        with self.assertRaises(Busy) as caught:
            self.runner.start(dict(self.item, path=empty))
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("would not", str(caught.exception))
        self.assertFalse(self.runner.running())
        game.stage.give_back_display.assert_called_once()

    def test_a_second_game_is_refused_while_one_runs(self):
        self.runner.start(self.item)
        with self.assertRaises(Busy):
            self.runner.start(self.item)


class Pads(GameTest):
    def test_the_buttons_a_phone_holds_reach_the_core(self):
        self.runner.start(self.item)
        phone = self.connect()
        phone.join()
        self.assertTrue(wait_for(lambda: phone.role == "p1"))
        phone.press(game.A | game.RIGHT)
        time.sleep(0.3)
        self.runner.stop()
        # libretro's A is bit 8 and RIGHT bit 7: 0x0180, little-endian.
        self.assertEqual(self.sram()[:2], b"\x80\x01")
        self.assertGreater(struct.unpack("<I", self.sram()[4:8])[0], 5)

    def test_the_second_phone_listens_until_it_asks_to_play(self):
        self.runner.start(self.item)
        first, second = self.connect(), self.connect()
        first.join()
        self.assertTrue(wait_for(lambda: first.role == "p1"))
        second.join()       # "auto" only ever takes player one
        time.sleep(0.1)
        self.assertEqual(second.role, "listener")
        second.join("any")
        self.assertTrue(wait_for(lambda: second.role == "p2"))
        self.assertEqual(self.runner.status()["seats"], [True, True])
        second.press(game.B)
        time.sleep(0.3)
        self.runner.stop()
        # Player two's B is libretro bit 0.
        self.assertEqual(self.sram()[2:4], b"\x01\x00")

    def test_a_one_player_game_has_no_second_seat(self):
        self.runner.start(dict(self.item, players=1))
        first, second = self.connect(), self.connect()
        first.join()
        self.assertTrue(wait_for(lambda: first.role == "p1"))
        second.join("any")
        time.sleep(0.1)
        self.assertEqual(second.role, "listener")

    def test_a_phone_that_goes_quiet_lets_go_of_its_buttons(self):
        """A locked screen sends no release. The watchdog lets go instead."""
        self.runner.start(self.item)
        phone = self.connect()
        phone.join()
        self.assertTrue(wait_for(lambda: phone.role == "p1"))
        phone.press(game.RIGHT)
        time.sleep(game.STALE_SECONDS + 0.6)
        self.runner.stop()
        self.assertEqual(self.sram()[:2], b"\x00\x00")

    def test_a_phone_that_hangs_up_frees_its_seat(self):
        self.runner.start(self.item)
        phone = self.connect()
        phone.join()
        self.assertTrue(wait_for(lambda: phone.role == "p1"))
        phone.hang_up()
        self.assertTrue(wait_for(
            lambda: self.runner.status()["seats"] == [False, False]))


class Sound(GameTest):
    def test_every_phone_hears_the_game_mixed_to_mono(self):
        self.runner.start(self.item)
        player, listener = self.connect(), self.connect()
        player.join()
        self.assertTrue(wait_for(lambda: player.audio and listener.audio))
        frame = player.audio[-1]
        samples = struct.unpack("<%dh" % (len(frame) // 2), frame)
        # 1000 left and 3000 right in the stub, so 2000 in the mix.
        self.assertTrue(samples)
        self.assertEqual(set(samples[1:]), {2000})
        # 800 samples a frame at 48 kHz is 400 at 24 kHz.
        self.assertTrue(390 <= len(samples) <= 410, len(samples))


if __name__ == "__main__":
    unittest.main()


class StageDispatch(unittest.TestCase):
    """stage.Stage is what the projectionist is handed. It picks the player
    by the item's kind, and a running game is a stage that is not idle."""

    def setUp(self):
        from playstick.stage import Stage
        self.player = mock.Mock()
        self.player.state.return_value = "idle"
        self.player.current_item.return_value = None
        self.game = mock.Mock()
        self.game.running.return_value = False
        self.game.current_item.return_value = None
        self.stage = Stage(self.player, self.game)

    def test_a_film_goes_to_mpv_and_a_game_to_the_frontend(self):
        self.stage.start({"title": "Ponyo"})
        self.player.start.assert_called_once()
        self.game.start.assert_not_called()
        self.stage.start({"title": "Stub", "kind": "game"})
        self.game.start.assert_called_once()

    def test_a_running_game_is_the_gaming_state(self):
        self.game.running.return_value = True
        self.game.current_item.return_value = {"title": "Stub"}
        self.assertEqual(self.stage.state(), "gaming")
        self.assertEqual(self.stage.current_title(), "Stub")
        with self.assertRaises(Busy):
            self.stage.start({"title": "Ponyo"})

    def test_stop_stops_both(self):
        self.stage.stop()
        self.game.stop.assert_called_once()
        self.player.stop.assert_called_once()
