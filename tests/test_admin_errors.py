"""The admin app saying why, when something fails: the share that is not
mounted, a write onto it that fails, and an unhandled error that would
otherwise only reach the terminal.

The core tests need no display. The ones that build a dialog run offscreen,
and are skipped where PySide6 is not installed.
"""

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin import log as log_setup                         # noqa: E402
from playstick_admin.core import games, nas_store, roms               # noqa: E402
from playstick_admin.core.db import Database                          # noqa: E402

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from playstick_admin import settings as settings_mod              # noqa: E402
    from playstick_admin.views import error_dialog, games_view        # noqa: E402
except ImportError:  # pragma: no cover -- no PySide6 here
    settings_mod = error_dialog = games_view = None


def nes():
    head = bytearray(16)
    head[:4] = b"NES\x1a"
    head[4] = 2
    return bytes(head) + b"\0" * (16384 * 2)


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def rom(self, name="Lan Master.nes"):
        path = os.path.join(self.dir, "roms", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(nes())
        return roms.read(path)


class ImportOntoABrokenShare(Temp):
    """A write that fails is a NasError for that file, not an OSError that
    ends the action -- the Games page reports the one and not the other."""

    def setUp(self):
        super().setUp()
        self.nas = os.path.join(self.dir, "nas")
        os.makedirs(self.nas)
        self.db = Database(os.path.join(self.dir, "admin.sqlite"))
        self.db.migrate()
        self.addCleanup(self.db.close)

    def test_a_staging_folder_that_cannot_be_made_is_a_nas_error(self):
        rom = self.rom()  # read before makedirs is patched
        with mock.patch.object(games.os, "makedirs", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(nas_store.NasError) as caught:
                games.import_rom(self.db, self.nas, rom)
        self.assertIn("could not write to the share", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_a_folder_on_the_share_that_cannot_be_made_is_a_nas_error(self):
        # The app's own folder is a file, so nothing can be made under it.
        # The write into staging works; the move onto the share does not.
        with open(os.path.join(self.nas, ".playstick"), "w"):
            pass
        with self.assertLogs("playstick_admin", "WARNING"):
            with self.assertRaises(nas_store.NasError) as caught:
                games.import_rom(self.db, self.nas, self.rom())
        self.assertIn("on the share", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(len(games.all_games(self.db)), 0, "no row for a game that is not there")

    def test_a_write_that_fails_is_a_nas_error(self):
        rom = self.rom()  # read before open() is patched
        with mock.patch("builtins.open", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(nas_store.NasError) as caught:
                games.import_rom(self.db, self.nas, rom)
        self.assertIn("No space left", str(caught.exception))


@unittest.skipIf(settings_mod is None, "needs PySide6")
class NasRootProblem(Temp):
    """Telling a share that is not mounted from one mounted read-only."""

    def problem(self, root):
        fake = type("S", (), {"nas_root": root})()
        return settings_mod.Settings.nas_root_problem(fake)

    def test_the_mount_point_is_the_nearest_mounted_parent(self):
        self.assertEqual(settings_mod._mount_point("/"), "/")
        deep = os.path.join(self.dir, "a", "b")
        os.makedirs(deep)
        found = settings_mod._mount_point(deep)
        self.assertTrue(os.path.ismount(found))
        self.assertTrue(deep.startswith(found) or
                        os.path.realpath(deep).startswith(found))

    def test_a_bare_mount_point_on_the_root_disk_says_the_nas_is_off(self):
        with mock.patch.object(settings_mod.os, "access", return_value=False), \
                mock.patch.object(settings_mod, "_mount_point", return_value="/"):
            problem = self.problem(self.dir)
        self.assertIn("Nothing is mounted at %s" % self.dir, problem)
        self.assertIn("Is the NAS on", problem)

    def test_a_read_only_mount_still_says_read_only(self):
        with mock.patch.object(settings_mod.os, "access", return_value=False), \
                mock.patch.object(settings_mod, "_mount_point", return_value="/mnt/nas"):
            problem = self.problem(self.dir)
        self.assertIn("is not writable", problem)

    def test_a_writable_root_is_fine(self):
        with mock.patch.object(settings_mod.os, "access", return_value=True):
            self.assertIsNone(self.problem(self.dir))


@unittest.skipIf(games_view is None, "needs PySide6")
class AddingGamesWithNoShare(Temp):
    """One sentence before the first file, not one refusal per file."""

    def tab(self, problem):
        settings = type("S", (), {"nas_root": self.dir,
                                  "nas_root_problem": lambda _self: problem})()
        tab = mock.Mock()
        tab._settings = settings
        return tab

    def test_nothing_is_imported_and_the_reason_is_shown(self):
        tab = self.tab("Nothing is mounted at /mnt/nas. Is the NAS on, and the share mounted?")
        found = [("a.nes", self.rom("a.nes")), ("b.nes", self.rom("b.nes"))]
        with mock.patch.object(games_view, "QMessageBox") as box, \
                mock.patch.object(games_view.games, "import_rom") as import_rom:
            games_view.GamesTab._import(tab, found)
        import_rom.assert_not_called()
        tab._push.assert_not_called()
        box.warning.assert_called_once()
        title, text = box.warning.call_args.args[1:]
        self.assertEqual(title, "No games were added")
        self.assertIn("Is the NAS on", text)

    def test_a_good_share_imports_as_before(self):
        tab = self.tab(None)
        with mock.patch.object(games_view, "QMessageBox") as box, \
                mock.patch.object(games_view.games, "import_rom", return_value="id1"):
            games_view.GamesTab._import(tab, [("a.nes", self.rom("a.nes"))])
        box.warning.assert_not_called()
        tab._push.assert_called_once_with(["id1"])


class ErrorListeners(unittest.TestCase):
    """log.py tells listeners about every unhandled error, after logging it."""

    def setUp(self):
        self.addCleanup(setattr, log_setup, "_listeners", list(log_setup._listeners))
        log_setup._listeners = []
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        self.addCleanup(setattr, threading, "excepthook", threading.excepthook)
        log_setup.install_hooks()
        self.heard = []
        log_setup.on_unhandled(lambda *a: self.heard.append(a))

    def raised(self, exc):
        try:
            raise exc
        except type(exc):
            return sys.exc_info()

    def test_an_error_on_the_gui_thread_is_told_after_it_is_logged(self):
        with self.assertLogs("playstick_admin", "ERROR"):
            sys.excepthook(*self.raised(ValueError("bad")))
        kind, value, _tb, where = self.heard[0]
        self.assertIs(kind, ValueError)
        self.assertEqual(str(value), "bad")
        self.assertEqual(where, threading.current_thread().name)

    def test_a_worker_threads_error_names_the_thread(self):
        def fail():
            raise RuntimeError("from the worker")
        worker = threading.Thread(target=fail, name="push-worker")
        with self.assertLogs("playstick_admin", "ERROR"):
            worker.start()
            worker.join()
        self.assertEqual(self.heard[0][3], "push-worker")

    def test_ctrl_c_and_exit_are_not_errors(self):
        with mock.patch.object(sys, "__excepthook__"):
            sys.excepthook(*self.raised(KeyboardInterrupt()))
        args = mock.Mock(exc_type=SystemExit)
        threading.excepthook(args)
        self.assertEqual(self.heard, [])

    def test_a_broken_listener_is_logged_and_the_rest_still_hear(self):
        log_setup._listeners.insert(0, mock.Mock(side_effect=RuntimeError("oops")))
        with self.assertLogs("playstick_admin", "ERROR") as logs:
            sys.excepthook(*self.raised(ValueError("bad")))
        self.assertTrue(any("an error listener failed" in m for m in logs.output))
        self.assertEqual(len(self.heard), 1)


@unittest.skipIf(error_dialog is None, "needs PySide6")
class ErrorDialog(unittest.TestCase):
    """The box: the error in one line, the traceback behind Show Details, and
    one box at a time however many arrive."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bridge = error_dialog._ErrorBridge()
        self.addCleanup(self.close)

    def close(self):
        if self.bridge._box is not None:
            self.bridge._box.done(0)
        self.app.processEvents()

    def report(self, exc, where="MainThread"):
        try:
            raise exc
        except type(exc):
            self.bridge.report(*sys.exc_info(), where)

    def test_it_opens_on_the_next_turn_of_the_event_loop(self):
        self.report(ValueError("no share"))
        self.assertIsNone(self.bridge._box, "queued, not opened inside the slot")
        self.app.processEvents()
        box = self.bridge._box
        self.assertEqual(box.windowTitle(), "Something went wrong")
        self.assertEqual(box.text(), "ValueError: no share")
        self.assertIn("Traceback", box.detailedText())
        self.assertIn("still running", box.informativeText())
        self.assertNotIn("(on ", box.informativeText())

    def test_an_error_with_no_message_is_its_name(self):
        self.report(KeyError())
        self.app.processEvents()
        self.assertEqual(self.bridge._box.text(), "KeyError")

    def test_a_worker_threads_error_says_which_thread(self):
        self.report(RuntimeError("x"), where="push-worker")
        self.app.processEvents()
        self.assertIn("(on push-worker)", self.bridge._box.informativeText())

    def test_more_errors_are_counted_on_the_open_box(self):
        self.report(ValueError("first"))
        self.app.processEvents()
        box = self.bridge._box
        self.report(ValueError("second"))
        self.report(ValueError("third"))
        self.app.processEvents()
        self.assertIs(self.bridge._box, box)
        self.assertEqual(box.text(), "ValueError: first")
        self.assertIn("2 more error(s) since", box.informativeText())

    def test_closing_it_lets_the_next_one_open(self):
        self.report(ValueError("first"))
        self.app.processEvents()
        self.bridge._box.done(0)
        self.app.processEvents()
        self.assertIsNone(self.bridge._box)
        self.report(ValueError("second"))
        self.app.processEvents()
        self.assertEqual(self.bridge._box.text(), "ValueError: second")
        self.assertNotIn("more error", self.bridge._box.informativeText())

    def test_install_registers_once(self):
        self.addCleanup(setattr, error_dialog, "_bridge", error_dialog._bridge)
        self.addCleanup(setattr, log_setup, "_listeners", list(log_setup._listeners))
        error_dialog._bridge = None
        log_setup._listeners = []
        first = error_dialog.install()
        self.assertIs(error_dialog.install(), first)
        self.assertEqual(log_setup._listeners, [first.report])


if __name__ == "__main__":
    unittest.main()
