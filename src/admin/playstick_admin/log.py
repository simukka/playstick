"""What the app is doing, said out loud on the terminal that started it.

`make admin` used to print nothing at all. The window would appear, and
everything after that -- a scan walking three mounts, a film being encoded, a
push to a stick that was not answering -- happened behind it, visible only in
whichever panel happened to be showing at the time and gone the moment it was
replaced. A queue left running overnight left no account of itself anywhere but
the `job.log` column.

So every background step logs, and the terminal is where it lands. That is not
a debugging aid bolted on: it is the only view of this app that is complete,
ordered, and still there in the morning.

WHY STDERR, AND WHY THAT MATTERS MORE THAN IT LOOKS

The preparer already writes its own log and its ffmpeg progress to stderr, and
it does something careful with the progress: one line redrawn in place on a
terminal, one line a minute when it is redirected to a file. Sharing the stream
is what keeps the two interleaved in the order they happened. A second stream
would give you two accounts that each look complete and disagree about what
followed what.

It also means `make admin 2>&1 | tee run.log` is the whole story, and that the
progress line degrades to something readable when you do.

LEVELS

  INFO   the default: what a person would want to know happened. A scan and
         what it found, a job starting and finishing, every write to the stick,
         every file that moved onto the share, everything that failed.
  DEBUG  every device request with its timing, every poll, the SQL-adjacent
         bookkeeping. Useful when the question is "did it even try".

Set PLAYSTICK_ADMIN_LOG=debug, or pass -v. -q leaves only warnings.

WHAT ELSE IS CAUGHT HERE

Three kinds of failure that used to disappear:

  an exception in a Qt slot, which PySide prints and then carries on from, so
  the window stays up and the reason scrolls past unlabelled;

  an exception in a worker thread that is not one of ours -- `threading` sends
  those to its own hook, which by default writes to stderr and is not seen
  again; and

  Qt's own warnings, which go to a C++ handler that knows nothing about any of
  this. "QThread: Destroyed while thread is still running" is a real bug being
  reported by the toolkit, and it deserves a timestamp next to the thing it
  happened after.
"""

import logging
import os
import sys
import threading

LOG = logging.getLogger("playstick_admin")

LEVELS = {"quiet": logging.WARNING, "warning": logging.WARNING,
          "info": logging.INFO, "normal": logging.INFO,
          "debug": logging.DEBUG, "verbose": logging.DEBUG}

# Long enough to be unambiguous overnight, short enough not to take a quarter of
# the line. No date: a run that spans midnight is rare and the file's mtime says
# which day it was.
TIME_FORMAT = "%H:%M:%S"


class _Formatter(logging.Formatter):
    """One line: the time, the level, where it came from, and what happened.

    The logger name is trimmed of the package prefix -- every line would
    otherwise begin with the same eighteen characters -- and the thread is named
    only when it is not the one drawing the window, because "this happened while
    you were doing something else" is the whole point of most of these lines.
    """

    def format(self, record):
        record.where = record.name
        for prefix in ("playstick_admin.core.", "playstick_admin.bridges.",
                       "playstick_admin.views.", "playstick_admin."):
            if record.where.startswith(prefix):
                record.where = record.where[len(prefix):]
                break
        # record.threadName is the thread that LOGGED, which is the one worth
        # naming; the thread doing the formatting is the same one, but only by
        # accident of how handlers work.
        record.aside = ("" if record.threadName == "MainThread"
                        else " (%s)" % record.threadName)
        return super().format(record)


def level_from(argv=None, env=None):
    """-v/-q on the command line, else PLAYSTICK_ADMIN_LOG, else INFO."""
    argv = sys.argv if argv is None else argv
    env = os.environ if env is None else env
    if "-v" in argv or "--verbose" in argv:
        return logging.DEBUG
    if "-q" in argv or "--quiet" in argv:
        return logging.WARNING
    wanted = (env.get("PLAYSTICK_ADMIN_LOG") or "").strip().lower()
    if wanted in LEVELS:
        return LEVELS[wanted]
    if wanted.isdigit():
        return int(wanted)
    return logging.INFO


def setup(argv=None, stream=None):
    """Point the root logger at stderr and return the level it settled on.

    The ROOT logger, not this package's: a library that logs -- urllib does,
    under `-v` -- is background activity too, and something that appears only in
    a debug build of somebody's head is not.
    """
    level = level_from(argv)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_Formatter(
        "%(asctime)s %(levelname)-7s %(where)s%(aside)s: %(message)s",
        TIME_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # urllib3 and friends are not here, but if a dependency ever arrives with an
    # opinion about DEBUG, it should not be able to bury the app's own lines.
    logging.getLogger("urllib3").setLevel(max(level, logging.INFO))
    return level


def install_hooks():
    """Send the three kinds of unhandled failure through the same channel."""

    def on_exception(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):      # Ctrl-C is not a fault
            sys.__excepthook__(kind, value, tb)
            return
        LOG.error("unhandled %s", kind.__name__, exc_info=(kind, value, tb))

    sys.excepthook = on_exception

    def on_thread_exception(args):
        if issubclass(args.exc_type, SystemExit):
            return
        LOG.error("unhandled %s on %s", args.exc_type.__name__,
                  args.thread.name if args.thread else "a worker thread",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = on_thread_exception


def install_qt_handler():
    """Route Qt's own messages into the same log.

    Imported here rather than at the top so this module stays usable -- and
    testable -- without a display.
    """
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    qt = logging.getLogger("playstick_admin.qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, _context, message):
        qt.log(levels.get(mode, logging.INFO), "%s", message)

    qInstallMessageHandler(handler)
