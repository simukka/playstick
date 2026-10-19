"""The preparation queue, in a table rather than in memory. Qt-free.

WHY IT IS PERSISTED

One film is an hour of ffmpeg and then a copy across a mount. A queue of twenty
is an evening and most of a night. An app that held that list in memory would
lose it to a closed window, a crash, or the laptop lid -- and lose it silently,
because there would be nothing left to say what had been asked for. Nobody
leaves an app like that running, which means nobody ever queues twenty.

Persisted, closing the window is a pause. That is the whole feature.

WHY A JOB IS NOT JUST item.state

Because they answer different questions and outlive each other. `item.state`
says what is true of a file now; a job says what somebody asked for and what
came of it -- its position, how many times it has been tried, the error, and
the log. A film already in the library can be queued again after a metadata
correction, which is one item and two jobs. Folding the two into one column is
the mistake the old index-plus-state-file pair made, and it is why nothing
could say why a film had failed.

ONE AT A TIME, AND NOT BY PREFERENCE

The preparer keeps its run state in module globals -- one reporter, one cancel
flag, one pair of ffmpeg paths -- so exactly one run may be in flight in this
process, ever. `claim_next` is written to make that a property of the table
rather than a rule somebody has to remember: it takes the next waiting job and
marks it running in one statement, and refuses if anything is already running.
"""

import logging
import time

WAITING = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# What a job is while it still counts as work. Nothing outside this set is
# looked at again unless somebody retries it.
PENDING = (WAITING, RUNNING)

LOG = logging.getLogger(__name__)


def enqueue(db, ident):
    """Ask for one film. Returns the job id, or None if one is already pending.

    Idempotent on purpose: selecting forty rows and pressing the button twice
    is one queue of forty, not two of forty interleaved. A film that failed can
    be queued again -- that is a retry, and it is the ordinary way back.
    """
    with db.write() as conn:
        existing = conn.execute(
            "SELECT id FROM job WHERE item_id = ? AND state IN (?, ?)",
            (ident, WAITING, RUNNING)).fetchone()
        if existing is not None:
            return None
        nxt = conn.execute(
            "SELECT COALESCE(MAX(pos), 0) + 1 AS n FROM job").fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO job (item_id, state, pos, queued_at) "
            "VALUES (?, ?, ?, ?)", (ident, WAITING, nxt, time.time()))
        conn.execute("UPDATE item SET state = 'queued' WHERE id = ?", (ident,))
        LOG.info("queued %s as job %d, position %d", ident, cur.lastrowid, nxt)
        return cur.lastrowid


def enqueue_many(db, idents):
    return [job for job in (enqueue(db, i) for i in idents) if job]


def claim_next(db):
    """Take the next waiting job and mark it running. Returns the row, or None.

    Refuses while anything is already running, which is the module docstring's
    single-run constraint expressed where it cannot be forgotten. The check and
    the claim are one transaction so that two callers cannot both win.
    """
    conn = db.write()
    # IMMEDIATE rather than the implicit deferred transaction: the read below
    # decides whether the write happens, so the lock has to be taken before the
    # read rather than upgraded after it. Deferred, two callers both read "none
    # running" and one of them fails to upgrade -- and this is exactly the shape
    # of race that only ever happens on somebody else's machine.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT id FROM job WHERE state = ? LIMIT 1",
                        (RUNNING,)).fetchone() is not None:
            conn.execute("ROLLBACK")
            return None
        row = conn.execute(
            "SELECT * FROM job WHERE state = ? ORDER BY pos LIMIT 1",
            (WAITING,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE job SET state = ?, started_at = ?, "
            "attempts = attempts + 1 WHERE id = ?",
            (RUNNING, time.time(), row["id"]))
        conn.execute("UPDATE item SET state = 'preparing' WHERE id = ?",
                     (row["item_id"],))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return db.one("SELECT * FROM job WHERE id = ?", (row["id"],))


def finish(db, job_id, item_state, error=None, log=None):
    """Close a job out. `error` decides whether it counts as done or failed."""
    with db.write() as conn:
        row = conn.execute("SELECT item_id FROM job WHERE id = ?",
                           (job_id,)).fetchone()
        conn.execute(
            "UPDATE job SET state = ?, error = ?, log = ?, finished_at = ? "
            "WHERE id = ?",
            (FAILED if error else DONE, error, log, time.time(), job_id))
        if row is not None:
            conn.execute("UPDATE item SET state = ? WHERE id = ?",
                         (item_state, row["item_id"]))
    if error:
        LOG.warning("job %s failed: %s", job_id, error)
    else:
        LOG.info("job %s is done (%s)", job_id, item_state)


def reset_running(db):
    """Put a job that was running when the process died back on the queue.

    Called once at startup. The work is idempotent -- every stage of it either
    finds its output already there and skips, or redoes it into a staging
    directory nothing else reads -- so the risk is doing an encode twice, and
    the alternative is a queue that stalls forever behind a job no thread is
    working on. Losing an evening silently is the worse of the two.

    Returns how many were reset.
    """
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE job SET state = ?, started_at = NULL WHERE state = ?",
            (WAITING, RUNNING))
        conn.execute(
            "UPDATE item SET state = 'queued' WHERE id IN "
            "(SELECT item_id FROM job WHERE state = ?)", (WAITING,))
    if cur.rowcount:
        LOG.warning("%d job(s) were left running by a process that died; "
                    "back on the queue", cur.rowcount)
    return cur.rowcount


def pending(db):
    """The queue as the panel draws it: waiting and running, in order, with
    enough of the item to name each one."""
    return db.query(
        "SELECT job.*, item.title, item.rel, item.size "
        "FROM job LEFT JOIN item ON item.id = job.item_id "
        "WHERE job.state IN (?, ?) ORDER BY job.pos", (WAITING, RUNNING))


def recent_failures(db, limit=50):
    return db.query(
        "SELECT job.*, item.title, item.rel "
        "FROM job LEFT JOIN item ON item.id = job.item_id "
        "WHERE job.state = ? ORDER BY job.finished_at DESC LIMIT ?",
        (FAILED, limit))


def unresolved_failures(db, limit=50):
    """Each film's last failure, while it is still a failure: not queued again
    since, and not prepared by a later attempt. What the queue table shows
    under the waiting jobs, and what "Retry failed" retries -- recent_failures
    would also offer a film that failed on Monday and succeeded on Tuesday."""
    return db.query(
        "SELECT job.*, item.title, item.rel "
        "FROM job JOIN item ON item.id = job.item_id "
        "WHERE job.state = ? AND item.state = ? "
        "AND job.id = (SELECT MAX(j.id) FROM job j "
        "              WHERE j.item_id = job.item_id AND j.state = ?) "
        "ORDER BY job.finished_at DESC LIMIT ?",
        (FAILED, FAILED, FAILED, limit))


def counts(db):
    return {row["state"]: row["n"] for row in
            db.query("SELECT state, COUNT(*) AS n FROM job GROUP BY state")}


def remove(db, job_id):
    """Take a waiting job off the queue, or a failed one off the table -- the
    film goes back to being one somebody can choose. A running one is left
    alone: stopping that is the worker's business, not the table's."""
    with db.write() as conn:
        row = conn.execute("SELECT item_id, state FROM job WHERE id = ?",
                           (job_id,)).fetchone()
        if row is None or row["state"] not in (WAITING, FAILED):
            return False
        conn.execute("DELETE FROM job WHERE id = ?", (job_id,))
        conn.execute(
            "UPDATE item SET state = 'available' WHERE id = ? AND state = ?",
            (row["item_id"], "queued" if row["state"] == WAITING else FAILED))
    return True


def move(db, job_id, delta):
    """Shuffle one waiting job up or down. Positions are rewritten across the
    whole waiting set rather than swapped, so a queue that has had things
    removed from the middle of it does not have gaps that make a swap a no-op.
    """
    order = [r["id"] for r in db.query(
        "SELECT id FROM job WHERE state = ? ORDER BY pos", (WAITING,))]
    if job_id not in order:
        return False
    at = order.index(job_id)
    to = max(0, min(len(order) - 1, at + delta))
    if to == at:
        return False
    order.insert(to, order.pop(at))
    with db.write() as conn:
        for pos, ident in enumerate(order, 1):
            conn.execute("UPDATE job SET pos = ? WHERE id = ?", (pos, ident))
    return True


def clear_finished(db):
    with db.write() as conn:
        cur = conn.execute("DELETE FROM job WHERE state IN (?, ?, ?)",
                           (DONE, FAILED, CANCELLED))
    return cur.rowcount
