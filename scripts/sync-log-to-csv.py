#!/usr/bin/env python3
"""Turn playstick sync telemetry out of the journal into a CSV.

The daemon writes one line per listening phone per second while that phone's
page has ?debug in its URL (see the docstring of Handler._log_sync, and the
README section "Collecting sync telemetry from a phone"). A ten minute film is
some six hundred lines per phone, which is past the point of reading them --
so this flattens them into a table that a spreadsheet or pandas can plot.

    ssh vivostick 'journalctl -u playstick-web --since "1 hour ago" -o short-iso' \\
      | ./scripts/sync-log-to-csv.py --summary > sync.csv

Reads named files, or stdin. Lines that are not sync telemetry are ignored, so
a whole unfiltered journal can be piped in. Input may be journalctl's default
"Aug 02 07:35:11 host tag[pid]:" form, --output=short-iso, --output=json, or
--output=cat (which carries no timestamp at all: the `time` column comes out
empty and only the phone's own `t` clock is available).

Prefer -o short-iso or -o json. The default syslog format omits the year, so
the year has to be guessed here -- see stamp_syslog.

THREE DERIVED COLUMNS are added; every other column is verbatim from the log.

  dt      seconds since the previous line from the same phone. The counts and
          peaks in each line describe the interval since the previous one, and
          that interval is NOT always a second: the page backs its poll off to
          5 s while the screen is locked, and iOS throttles the timer further
          still. Divide `w`, `ls`, `sk`, `wt` and `bf` by this before comparing
          two stretches of a film, or a pocketed phone will look calm.
  ctpos   (ct - pos) in ms: the element's clock against mpv's, as the two were
          reported in this one line. It is a coarse cross-check on `err` and
          not a replacement -- it lacks the RTT and track-offset corrections
          the page applies, so expect a constant bias of tens of ms. What it
          is good for is catching the case where `err` looks healthy because
          the page's own clock model has come adrift.
  gap     1 when this phone skipped at least one poll before this line (dt >
          1.8 s). Marks the boundaries you should not read a trend across.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta

# Everything before the blob is the daemon's own account, and is positional.
SYNC_RE = re.compile(
    r"playstick:\s+sync\s+(?P<ip>\S+)\s+(?P<state>\S+)\s+"
    r"pos=(?P<pos>\S+)\s+buf=(?P<buf>\d+)\s+(?P<blob>\S+)")

ISO_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:[+-]\d{2}:?\d{2}|Z)?)\s+(?P<host>\S+)\s")
SYSLOG_RE = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"\s+(?P<host>\S+)\s")

# The order the phone sends them in, which is also the order they are worth
# reading in: who and when, what state, the two clocks, the symptom, the
# buffer, the controller, the network. Any field not in this list -- a newer
# page than this script -- is appended, sorted, rather than dropped.
COLUMNS = [
    "time", "host", "ip", "id", "t", "dt", "gap",
    "state", "st", "hid",
    "pos", "ct", "ctpos", "err", "errp", "trim",
    "lag", "ls", "wt", "sk",
    "ahead", "amin", "rs", "nb", "buf", "bf",
    "rate", "drift", "step", "ns", "w", "dw",
    "rtt", "tun", "v",
]
DERIVED = ("time", "host", "ip", "state", "pos", "buf", "dt", "gap", "ctpos")

# A poll is due every second while playing. Past this, the page was throttled
# or the phone was off the air, and the counters in the line cover the whole
# absence.
GAP_AFTER = 1.8


def stamp_iso(text):
    """journalctl -o short-iso, and near enough anything else ISO-shaped."""
    text = text.replace(",", ".").replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # +0300 -> +03:00; fromisoformat before 3.11 will not take the compact form
    # and the stick runs whatever Debian ships.
    m = re.search(r"([+-]\d{2})(\d{2})$", text)
    if m:
        text = text[:m.start()] + m.group(1) + ":" + m.group(2)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def stamp_syslog(text, now=None):
    """journalctl's default format, which does not print the year.

    Guessed as the current year, then walked back one if that lands in the
    future -- which is what a December film read in January looks like. The
    guess is only ever wrong by a whole year and only for logs older than one,
    but it is a guess, which is the argument for -o short-iso.
    """
    now = now or datetime.now()
    text = re.sub(r"\s+", " ", text.strip())
    try:
        when = datetime.strptime("%d %s" % (now.year, text),
                                 "%Y %b %d %H:%M:%S.%f" if "." in text
                                 else "%Y %b %d %H:%M:%S")
    except ValueError:
        return None
    if when - now > timedelta(days=1):
        when = when.replace(year=now.year - 1)
    return when


def unwrap(line):
    """One input line -> (timestamp or None, host or '', the message).

    Handles the three journalctl formats worth using plus -o cat, so that an
    unfiltered journal can be piped in without the caller having to say which
    one it is.
    """
    line = line.rstrip("\n")
    if line[:1] == "{":
        try:
            rec = json.loads(line)
        except ValueError:
            return None, "", line
        msg = rec.get("MESSAGE", "")
        if isinstance(msg, list):        # journalctl renders non-UTF-8 as bytes
            msg = bytes(msg).decode("utf-8", "replace")
        when = rec.get("__REALTIME_TIMESTAMP")
        try:
            when = datetime.fromtimestamp(int(when) / 1e6)
        except (TypeError, ValueError):
            when = None
        return when, rec.get("_HOSTNAME", ""), msg
    m = ISO_RE.match(line)
    if m:
        return stamp_iso(m.group("ts")), m.group("host"), line[m.end():]
    m = SYSLOG_RE.match(line)
    if m:
        return stamp_syslog(m.group("ts")), m.group("host"), line[m.end():]
    return None, "", line


def parse(line):
    """One input line -> a row dict, or None if it is not sync telemetry."""
    when, host, msg = unwrap(line)
    m = SYNC_RE.search(msg)
    if not m:
        return None
    row = {
        "time": when.isoformat(timespec="milliseconds") if when else "",
        "host": host,
        "ip": m.group("ip"),
        "state": m.group("state"),
        # The daemon writes pos=? when mpv has not reported a position yet.
        "pos": "" if m.group("pos") == "?" else m.group("pos"),
        "buf": m.group("buf"),
    }
    for pair in m.group("blob").split(";"):
        key, sep, value = pair.partition("=")
        # No '=' means the daemon's length cap landed mid-pair. Drop it rather
        # than inventing a column named after half a value.
        if sep and key and key not in DERIVED:
            row[key] = value
    return row


def number(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def derive(rows):
    """Fill in dt, gap and ctpos. Needs the previous line from the same phone.

    Keyed on ip AND id: one phone reloading the page restarts `t` at zero, and
    two phones behind the same NAT would otherwise be interleaved into one
    nonsensical series.
    """
    last = {}
    for row in rows:
        who = (row.get("ip", ""), row.get("id", ""))
        t = number(row, "t")
        prev = last.get(who)
        if t is not None and prev is not None and t >= prev:
            dt = t - prev
            row["dt"] = "%.1f" % dt
            row["gap"] = "1" if dt > GAP_AFTER else "0"
        if t is not None:
            last[who] = t
        ct, pos = number(row, "ct"), number(row, "pos")
        if ct is not None and pos is not None:
            row["ctpos"] = "%d" % round((ct - pos) * 1000)
    return rows


def summarise(rows, out):
    """A digest to stderr: enough to tell whether the capture is worth keeping.

    Written before anybody opens the CSV, because the common outcome of a first
    capture is that the phone was not actually listening, or the film was too
    short, and that is cheaper to learn here.
    """
    phones = {}
    for row in rows:
        phones.setdefault((row.get("ip", ""), row.get("id", "")), []).append(row)
    print("%d telemetry lines from %d phone(s)" % (len(rows), len(phones)),
          file=out)
    head = ("phone", "lines", "span", "play", "stalls", "worst lag",
            "min buf", "rate writes", "gaps")
    print("  %-22s %6s %8s %6s %7s %10s %8s %12s %6s" % head, file=out)
    for (ip, pid), group in sorted(phones.items()):
        played = [r for r in group if r.get("st") == "play"]
        span = max((number(r, "t") or 0) for r in group) - \
            min((number(r, "t") or 0) for r in group)
        stalls = sum(int(number(r, "ls") or 0) for r in group)
        writes = sum(int(number(r, "w") or 0) for r in group)
        gaps = sum(1 for r in group if r.get("gap") == "1")
        lags = [number(r, "lag") for r in group if number(r, "lag") is not None]
        # The low-water mark is the point of amin; a mean would hide it. A
        # phone that never played has neither, and a dash says so -- printing
        # a zero there would read as "no buffer at all", which is the alarming
        # answer to a question that was not asked.
        mins = [number(r, "amin") for r in group
                if number(r, "amin") is not None]
        print("  %-22s %6d %7.0fs %6d %7d %10s %8s %12d %6d" % (
            ("%s/%s" % (ip, pid))[:22], len(group), span, len(played), stalls,
            "%.0f ms" % max(lags) if lags else "-",
            "%.1fs" % min(mins) if mins else "-", writes, gaps), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Flatten playstick sync telemetry from the journal to CSV.",
        epilog="Feed it journalctl -o short-iso (or -o json); the default "
               "journalctl format has no year in it and has to be guessed.")
    ap.add_argument("files", nargs="*", metavar="FILE",
                    help="journal text; stdin if none given")
    ap.add_argument("-o", "--out", metavar="FILE",
                    help="write here instead of stdout")
    ap.add_argument("--id", metavar="ID", action="append", dest="ids",
                    help="keep only this phone's page load (repeatable)")
    ap.add_argument("--playing", action="store_true",
                    help="keep only lines where the phone was playing")
    ap.add_argument("--summary", action="store_true",
                    help="print a digest to stderr as well")
    args = ap.parse_args(argv)

    rows, skipped = [], 0
    streams = args.files or ["-"]
    for name in streams:
        handle = sys.stdin if name == "-" else open(name, "r", errors="replace")
        try:
            for line in handle:
                row = parse(line)
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
        finally:
            if handle is not sys.stdin:
                handle.close()

    # Ordered before deriving: journalctl -f into a file arrives in order, but
    # several -u units or several files concatenated do not, and dt across an
    # out-of-order pair would be negative or enormous.
    rows.sort(key=lambda r: (r.get("ip", ""), r.get("id", ""),
                             number(r, "t") if number(r, "t") is not None
                             else 0.0))
    derive(rows)
    if args.ids:
        rows = [r for r in rows if r.get("id") in args.ids]
    if args.playing:
        rows = [r for r in rows if r.get("st") == "play"]
    # Back into wall order for the file itself: that is how it was recorded and
    # how it will be plotted. Lines without a timestamp (-o cat) keep the
    # per-phone order established above.
    rows.sort(key=lambda r: (r.get("time", ""), r.get("ip", ""),
                             number(r, "t") or 0.0))

    extra = sorted({k for r in rows for k in r} - set(COLUMNS))
    columns = COLUMNS + extra
    out = open(args.out, "w", newline="") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if out is not sys.stdout:
            out.close()

    if args.summary:
        if extra:
            print("newer fields than this script knows: %s" % ", ".join(extra),
                  file=sys.stderr)
        summarise(rows, sys.stderr)
        print("%d non-telemetry line(s) ignored" % skipped, file=sys.stderr)
    if not rows:
        print("no sync telemetry found -- was the page opened with ?debug, "
              "and was the phone actually listening?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
