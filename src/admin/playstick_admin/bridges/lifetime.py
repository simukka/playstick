"""Keeping a worker alive for as long as the thread that is running it.

Every worker in this package is built the same way: a QObject with no parent,
moved onto a fresh QThread, started, and left to signal its way home. It cannot
have a parent -- an object with one cannot be moved to another thread -- so the
only thing keeping it alive is a Python name, and the caller's name goes out of
scope the moment its `start` returns.

What happens then is a garbage collector's timing. The wrapper is collected, the
C++ object with it, and the thread starts a job that no longer exists: no
result, no failure, no `finished`, ever. It is not a crash and it is not a hang
anybody sees as one -- the button just does nothing, once, and works the next
time.

That "next time" is what hid it for so long. It showed up as a status label that
went stale now and then, a TMDb search that came back empty until you pressed it
again -- and, in the device poll, as an indicator that stopped updating for good,
because the poll will not start a second call while it believes one is running.

So every worker is registered here instead, and dropped when its thread says it
has finished. There is no policy in this file and nothing reads the set: it is a
strong reference and a place to put it.
"""

import threading

# (worker, thread) for everything currently running.
_LIVE = set()


def hold(worker, thread):
    """Keep both alive until the thread finishes. Call it once, in the `start`
    that pairs them, before the caller has a chance to drop either."""
    pair = (worker, thread)
    _LIVE.add(pair)
    thread.finished.connect(lambda: _LIVE.discard(pair))


def running():
    """How many workers are in flight. For the log line that says what the app
    is still doing, and for anybody wondering whether one leaked."""
    return len(_LIVE)


def name_thread(label):
    """Give the thread a name the log can print.

    Python sees a QThread running its code as a `_DummyThread` called
    "Dummy-3", which tells a reader only that it was not the UI thread. Every
    worker calls this first thing in its `run`, so a line about a poll that
    landed in the middle of an encode says which of them it came from.
    """
    try:
        threading.current_thread().name = label
    except Exception:               # pragma: no cover - never worth a crash
        pass
