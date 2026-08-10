#!/usr/bin/env python3
"""Plot playstick sync telemetry: one page, every metric, one time axis.

    ssh vivostick 'journalctl -u playstick-web --since "20 min ago" -o short-iso' \\
      | ./scripts/sync-log-plot.py -o sync.html

Takes the CSV that sync-log-to-csv.py writes, or the raw journal (it will run
the same parser itself), and writes a standalone HTML file with an inline SVG.
No libraries, no network, nothing to install -- it opens in a browser and it
survives being emailed to somebody.

WHY EVERYTHING SHARES ONE X AXIS. The faults in this system are not visible in
any single series. A dropout is a coincidence between series: the element lost
time AND the rate was written, or it lost time AND the buffer collapsed, or it
lost time and neither. Reading that off two separate plots is guesswork, so
every panel is drawn against the same seconds-since-page-load and the hover
band reports all of them at once.

Counters are per-interval (see `dt` in sync-log-to-csv.py). Where a poll was
skipped the interval is longer, and those stretches are shaded: a spike in a
count there may only mean the interval was five seconds instead of one.
"""

import argparse
import csv
import html
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

W = 1180                  # plot width, px
LEFT, RIGHT = 66, 18
PANEL_H = 122
PANEL_GAP = 26
STRIP_H = 34
TOP = 34

# Series are drawn in this order, so the ones being compared sit on top.
PANELS = [
    {
        "title": "sync error — sound minus picture (ms)",
        "note": "negative is sound behind. Shaded: where a listener stops noticing.",
        "series": [
            {"key": "err", "label": "err", "colour": "#2563eb", "kind": "line"},
            {"key": "errp", "label": "errp (peak)", "colour": "#93c5fd", "kind": "line"},
            {"key": "ctpos", "label": "ct−pos", "colour": "#a3a3a3", "kind": "line",
             "dash": "3,3"},
        ],
        # The eye starts noticing at about +45 ms and about -125 ms. Drawing the
        # band is the difference between "the error moved" and "the error left
        # the region where it does not matter".
        "band": (-125, 45),
        "zero": True,
    },
    {
        "title": "element clock loss (ms)",
        "note": "worst shortfall in one 250 ms tick. 16–21 ms is AAC frame "
                "quantisation, not a dropout.",
        "series": [
            {"key": "lag", "label": "lag", "colour": "#dc2626", "kind": "line"},
        ],
        "rule": (30, "STALL threshold"),
        "floor": 0,
    },
    {
        "title": "counts per interval",
        "note": "divide by dt before comparing stretches — see the shaded gaps.",
        "series": [
            {"key": "ls", "label": "stalls", "colour": "#dc2626", "kind": "bar"},
            {"key": "w", "label": "rate writes", "colour": "#ea580c", "kind": "bar"},
            {"key": "sk", "label": "seeks", "colour": "#7c3aed", "kind": "bar"},
            {"key": "wt", "label": "waiting/stalled", "colour": "#0891b2", "kind": "bar"},
            {"key": "bf", "label": "mpv buffering", "colour": "#65a30d", "kind": "bar"},
        ],
        "floor": 0,
    },
    {
        "title": "playbackRate (ppm)",
        "note": "every write costs ~43 ms of audio on iOS. At the clamp the "
                "integrator is frozen. On a v=2 capture `ratio` is what was "
                "measured between the two machines and `drift` is only what "
                "was left inside the phone -- a drift that grows to look like "
                "the ratio is a measurement not reaching the element.",
        "series": [
            {"key": "rate", "label": "rate", "colour": "#ea580c", "kind": "line"},
            {"key": "ratio", "label": "ratio (measured)", "colour": "#7c3aed",
             "kind": "line"},
            {"key": "drift", "label": "drift", "colour": "#f59e0b", "kind": "line"},
            {"key": "dw", "label": "largest write", "colour": "#fca5a5", "kind": "bar"},
        ],
        "rule": (20000, "RATE_LIMIT"),
        "rule2": (-20000, None),
        "zero": True,
    },
    {
        "title": "buffer ahead of the play head (s)",
        "note": "collapsing toward zero is starvation; anything else is not.",
        "series": [
            {"key": "ahead", "label": "ahead", "colour": "#0891b2", "kind": "line"},
            {"key": "amin", "label": "amin (low-water)", "colour": "#67e8f9", "kind": "line"},
        ],
        "floor": 0,
    },
    {
        "title": "network and model",
        "note": "rtt is the status poll; ort is the /api/time round trip the "
                "clock offset came out of, and half of it is the error bar on "
                "that offset. ns is samples in the window.",
        "series": [
            {"key": "rtt", "label": "rtt ms", "colour": "#6b7280", "kind": "line"},
            {"key": "ort", "label": "ort ms", "colour": "#0d9488", "kind": "line"},
            {"key": "ns", "label": "ns", "colour": "#16a34a", "kind": "line"},
            {"key": "dt", "label": "dt s", "colour": "#d1d5db", "kind": "line"},
        ],
        "floor": 0,
    },
    {
        # A v=1 capture draws nothing here, which is right: it had no timecode
        # and no notion of a timeline that could change under it.
        "title": "the timeline being followed",
        "note": "ep steps at every discontinuity the daemon saw -- a pause, a "
                "resume, a buffering stall, a film change. tcage is how old "
                "the anchor being extrapolated was, in ms; it should sit "
                "around one poll and a jump in it means polls were missed.",
        "series": [
            {"key": "ep", "label": "epoch", "colour": "#be123c", "kind": "line"},
            {"key": "tcage", "label": "anchor age ms", "colour": "#fb7185",
             "kind": "line"},
        ],
        "floor": 0,
    },
]

# The state strip. Not a chart: what it answers is "was it even playing", which
# is the first thing to check and the easiest to forget.
STATE_COLOURS = {
    "play": "#16a34a", "pause": "#f59e0b", "idle": "#9ca3af", "off": "#e5e7eb",
    "": "#e5e7eb",
}


def load_rows(stream, name):
    """CSV from sync-log-to-csv.py, or the journal it was made from."""
    text = stream.read()
    first = text.split("\n", 1)[0]
    if first.startswith("time,") or ",id," in first:
        return list(csv.DictReader(io.StringIO(text)))
    # Not a CSV. Rather than telling the caller to go and run the other script,
    # run it here: a capture usually gets replotted several times and a second
    # step is a second thing to get wrong.
    spec = importlib.util.spec_from_file_location(
        "sync_log_to_csv", os.path.join(HERE, "sync-log-to-csv.py"))
    tool = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(tool)
    rows = [r for r in (tool.parse(line) for line in text.splitlines()) if r]
    rows.sort(key=lambda r: (r.get("ip", ""), r.get("id", ""),
                             tool.number(r, "t") or 0.0))
    tool.derive(rows)
    if not rows:
        sys.exit("%s: no sync telemetry in there" % name)
    return rows


def num(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def pick_phone(rows, want):
    """One page is one phone: two of them share no clock and no page load."""
    groups = {}
    for r in rows:
        groups.setdefault((r.get("ip", ""), r.get("id", "")), []).append(r)
    if want:
        for key, group in groups.items():
            if key[1] == want:
                return key, group, groups
        sys.exit("no phone with id=%s (have: %s)"
                 % (want, ", ".join(sorted(k[1] for k in groups))))
    key = max(groups, key=lambda k: len(groups[k]))
    return key, groups[key], groups


def robust_range(vals):
    """(lo, hi, clipped): the range the bulk of the data lives in.

    A film's worth of telemetry usually contains one enormous startup value --
    the error before the first placement, an rtt while the radio wakes up --
    and letting it set the axis flattens the next four minutes into a
    horizontal line. So the tails are dropped, but only when they really are
    tails: a series that is genuinely spread over its whole range keeps it.
    Anything outside is drawn clamped to the edge, which reads as "off the top"
    rather than silently disappearing.
    """
    vs = sorted(vals)
    if len(vs) < 20:
        return vs[0], vs[-1], False
    # Tukey fences rather than a percentile trim. A capture that restarted a
    # film twice carries a dozen enormous values, and a 1st-percentile cut just
    # lands inside them -- the fences are set by the middle half of the data
    # and do not move when the tail gets longer.
    q1, q3 = vs[len(vs) // 4], vs[3 * len(vs) // 4]
    iqr = q3 - q1
    if iqr <= 0:
        return vs[0], vs[-1], False
    lo, hi = max(vs[0], q1 - 3 * iqr), min(vs[-1], q3 + 3 * iqr)
    return lo, hi, lo > vs[0] or hi < vs[-1]


def nice_bounds(lo, hi, floor=None):
    """A range that a human can read ticks off, with the data inside it."""
    if lo is None:
        return 0.0, 1.0
    if floor is not None:
        lo = min(lo, floor)
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    step = 10 ** int(round(__import__("math").log10(span / 4)))
    for mult in (1, 2, 2.5, 5, 10):
        if span / (step * mult) <= 5:
            step *= mult
            break
    lo = step * (lo // step)
    hi = step * -(-hi // step)
    return lo, hi


def ticks(lo, hi, count=4):
    step = (hi - lo) / count
    return [lo + i * step for i in range(count + 1)]


def fmt(v):
    if abs(v) >= 10000:
        return "%.0fk" % (v / 1000.0)
    if abs(v) >= 100 or v == int(v):
        return "%.0f" % v
    return "%.1f" % v


class Svg:
    def __init__(self):
        self.out = []

    def add(self, s):
        self.out.append(s)

    def rect(self, x, y, w, h, fill, **kw):
        extra = "".join(' %s="%s"' % (k.replace("_", "-"), v) for k, v in kw.items())
        self.add('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"%s/>'
                 % (x, y, max(w, 0), max(h, 0), fill, extra))

    def line(self, x1, y1, x2, y2, stroke, width=1, dash=None):
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        self.add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="%s"%s/>' % (x1, y1, x2, y2, stroke, width, d))

    def path(self, points, stroke, width=1.4, dash=None):
        if len(points) < 2:
            if len(points) == 1:
                self.add('<circle cx="%.1f" cy="%.1f" r="1.6" fill="%s"/>'
                         % (points[0][0], points[0][1], stroke))
            return
        d = "M" + " L".join("%.1f %.1f" % p for p in points)
        dash = ' stroke-dasharray="%s"' % dash if dash else ""
        self.add('<path d="%s" fill="none" stroke="%s" stroke-width="%s" '
                 'stroke-linejoin="round"%s/>' % (d, stroke, width, dash))

    def text(self, x, y, s, cls="", anchor="start"):
        self.add('<text x="%.1f" y="%.1f" class="%s" text-anchor="%s">%s</text>'
                 % (x, y, cls, anchor, html.escape(str(s))))

    def __str__(self):
        return "\n".join(self.out)


def clip(rows, args):
    """Rows inside --start/--end, and the x range to draw them against.

    Done once, before anything is counted: a header that reported the whole
    capture over a plot of thirty seconds of it would be a quiet way to draw
    the wrong conclusion.
    """
    xs = [num(r, "t") for r in rows if num(r, "t") is not None]
    if not xs:
        sys.exit("no usable `t` column -- is this the right CSV?")
    x0 = args.start if args.start is not None else min(xs)
    x1 = args.end if args.end is not None else max(xs)
    rows = [r for r in rows
            if num(r, "t") is not None and x0 <= num(r, "t") <= x1]
    if not rows:
        sys.exit("nothing inside --start/--end")
    return rows, x0, (x1 if x1 - x0 > 1e-6 else x0 + 1)


def draw(rows, x0, x1):
    plot_w = W - LEFT - RIGHT
    sx = lambda t: LEFT + (t - x0) / (x1 - x0) * plot_w

    height = TOP + STRIP_H + PANEL_GAP + len(PANELS) * (PANEL_H + PANEL_GAP) + 30
    s = Svg()

    # Gap shading first, so it sits under everything. A gap means the counters
    # on the line after it describe a longer interval than one second.
    gaps = []
    for i, r in enumerate(rows):
        if r.get("gap") == "1" and i:
            gaps.append((sx(num(rows[i - 1], "t")), sx(num(r, "t"))))
    for a, b in gaps:
        s.rect(a, TOP, b - a, height - TOP - 30, "var(--gap)")

    # The state strip.
    y = TOP
    s.text(LEFT, y - 8, "state", cls="ttl")
    for i, r in enumerate(rows):
        a = sx(num(r, "t"))
        b = sx(num(rows[i + 1], "t")) if i + 1 < len(rows) else a + 2
        st = r.get("st", "")
        s.rect(a, y, max(b - a, 1.0), STRIP_H - 14,
               STATE_COLOURS.get(st, "#e5e7eb"))
        if r.get("hid") == "1":
            # Screen locked or page backgrounded: the poll backs off to 5 s and
            # the correction loop is not running at all.
            s.rect(a, y + STRIP_H - 13, max(b - a, 1.0), 5, "#1f2937")
    s.text(LEFT + plot_w, y - 8,
           "green play · amber pause · grey idle/off · dark bar = screen off",
           cls="note", anchor="end")
    y += STRIP_H + PANEL_GAP

    for panel in PANELS:
        s.text(LEFT, y - 8, panel["title"], cls="ttl")
        s.text(LEFT + plot_w, y - 8, panel.get("note", ""), cls="note",
               anchor="end")

        vals = [v for ser in panel["series"] for v in
                (num(r, ser["key"]) for r in rows) if v is not None]
        vlo, vhi, cut = robust_range(vals) if vals else (None, 1.0, False)
        if panel.get("rule") and vals:
            # The clamp only belongs on the axis if the data goes anywhere near
            # it; forcing it in flattens everything else against the baseline.
            if max(abs(v) for v in vals) > abs(panel["rule"][0]) * 0.4:
                vlo, vhi = min(vlo, panel["rule"][0]), max(vhi, panel["rule"][0])
                if panel.get("rule2"):
                    vlo = min(vlo, panel["rule2"][0])
                    vhi = max(vhi, panel["rule2"][0])
        lo, hi = nice_bounds(vlo, vhi, panel.get("floor"))
        top, bottom = y, y + PANEL_H
        sy = lambda v: min(bottom, max(
            top, y + PANEL_H - (v - lo) / (hi - lo) * PANEL_H))
        if cut:
            # Inside the panel, not under it: under it is the next panel's
            # title, and a caption that lands on somebody else's chart is worse
            # than no caption.
            s.text(LEFT + plot_w - 6, y + PANEL_H - 9,
                   "y clipped — outliers drawn on the edge",
                   cls="note", anchor="end")

        s.rect(LEFT, y, plot_w, PANEL_H, "var(--panel)")
        if panel.get("band"):
            b0, b1 = panel["band"]
            s.rect(LEFT, sy(min(b1, hi)), plot_w,
                   abs(sy(max(b0, lo)) - sy(min(b1, hi))), "var(--ok)")
        for t in ticks(lo, hi):
            s.line(LEFT, sy(t), LEFT + plot_w, sy(t), "var(--grid)")
            s.text(LEFT - 8, sy(t) + 3.5, fmt(t), cls="ax", anchor="end")
        if panel.get("zero") and lo < 0 < hi:
            s.line(LEFT, sy(0), LEFT + plot_w, sy(0), "var(--zero)")
        for which in ("rule", "rule2"):
            if panel.get(which) and lo <= panel[which][0] <= hi:
                v, label = panel[which]
                s.line(LEFT, sy(v), LEFT + plot_w, sy(v), "var(--rule)", 1, "5,4")
                if label:
                    s.text(LEFT + 4, sy(v) - 4, label, cls="rulelab")

        bars = [ser for ser in panel["series"] if ser["kind"] == "bar"]
        for bi, ser in enumerate(bars):
            # Impulses rather than filled bars: they are counts at an instant,
            # and side-by-side lets five of them share a panel legibly.
            off = (bi - (len(bars) - 1) / 2.0) * 2.2
            for r in rows:
                v = num(r, ser["key"])
                if v:
                    x = sx(num(r, "t")) + off
                    s.line(x, sy(max(lo, 0)), x, sy(v), ser["colour"], 1.8)
        for ser in panel["series"]:
            if ser["kind"] != "line":
                continue
            run = []
            for r in rows:
                v = num(r, ser["key"])
                if v is None:
                    # A break, not a straight line across it: joining two sides
                    # of a pause would draw a trend that never happened.
                    s.path(run, ser["colour"], dash=ser.get("dash"))
                    run = []
                else:
                    run.append((sx(num(r, "t")), sy(v)))
            s.path(run, ser["colour"], dash=ser.get("dash"))

        lx = LEFT + 6
        for ser in panel["series"]:
            s.rect(lx, y + PANEL_H - 13, 9, 3, ser["colour"])
            s.text(lx + 13, y + PANEL_H - 9, ser["label"], cls="leg")
            lx += 13 + 6.2 * len(ser["label"]) + 12
        y += PANEL_H + PANEL_GAP

    # X axis, once, under everything.
    for i in range(7):
        t = x0 + (x1 - x0) * i / 6.0
        s.line(sx(t), TOP, sx(t), y - PANEL_GAP, "var(--grid)")
        s.text(sx(t), y - PANEL_GAP + 16, "%.0fs" % t, cls="ax", anchor="middle")
    s.text(LEFT + plot_w / 2, y + 8, "seconds since the page loaded",
           cls="ax", anchor="middle")

    # One hover band per sample, on top, carrying every value for that instant.
    # This is why there is no JavaScript: an SVG <title> is a tooltip already.
    all_keys = ["st", "dt", "pos", "ct", "ctpos", "err", "errp", "lag", "ls",
                "w", "dw", "sk", "wt", "bf", "ahead", "amin", "rate", "drift",
                "step", "ns", "rtt", "rs", "nb", "hid", "buf", "trim", "tun"]
    for i, r in enumerate(rows):
        a = sx(num(r, "t"))
        b = sx(num(rows[i + 1], "t")) if i + 1 < len(rows) else a + 3
        parts = ["t=%.1f  %s" % (num(r, "t"), r.get("time", "")[11:19])]
        parts += ["%s=%s" % (k, r[k]) for k in all_keys if r.get(k) not in (None, "")]
        # fill="none" with pointer-events="all", not fill="transparent":
        # `transparent` is a CSS colour keyword that SVG paint does not define,
        # and a renderer that does not take it paints the band BLACK -- over
        # every panel, since this layer is on top.
        s.add('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
              'pointer-events="all" class="hov"><title>%s</title></rect>'
              % (a - 1, TOP, max(b - a, 2.0), y - PANEL_GAP - TOP,
                 html.escape("\n".join(parts))))

    return str(s), height + 20


def page(rows, key, groups, x0, x1):
    body, height = draw(rows, x0, x1)
    ip, pid = key
    span = [num(r, "t") for r in rows if num(r, "t") is not None]
    others = [k[1] for k in groups if k != key]
    stalls = sum(int(num(r, "ls") or 0) for r in rows)
    writes = sum(int(num(r, "w") or 0) for r in rows)
    seeks = sum(int(num(r, "sk") or 0) for r in rows)
    head = ("%s · page load %s · %d lines over %.0f s · %d stalls, %d rate "
            "writes, %d seeks" % (ip, pid, len(rows), max(span) - min(span),
                                  stalls, writes, seeks))
    note = ("other phones in this capture: " + ", ".join(others) +
            " — plot them with --id") if others else ""
    return TEMPLATE % {
        "title": "playstick sync — %s" % pid,
        "head": html.escape(head),
        "note": html.escape(note),
        "w": W, "h": height, "svg": body,
        "when": html.escape(rows[0].get("time", "") or "no timestamps"),
    }


TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
  :root {
    --bg: #ffffff; --fg: #111827; --muted: #6b7280;
    --panel: #f9fafb; --grid: #e5e7eb; --zero: #9ca3af; --rule: #ef4444;
    --ok: #dcfce7; --gap: #fef3c7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0b0f14; --fg: #e5e7eb; --muted: #9ca3af;
      --panel: #111827; --grid: #1f2937; --zero: #4b5563; --rule: #f87171;
      --ok: #14532d; --gap: #422006;
    }
  }
  body { margin: 0; padding: 22px; background: var(--bg); color: var(--fg);
         font: 14px/1.5 -apple-system, system-ui, "Segoe UI", sans-serif; }
  h1 { font-size: 17px; margin: 0 0 2px; font-weight: 650; }
  p  { margin: 0 0 4px; color: var(--muted); font-size: 12.5px; }
  .wrap { overflow-x: auto; }
  svg { display: block; }
  text { fill: var(--fg); font: 11px -apple-system, system-ui, sans-serif; }
  .ttl { font-size: 12.5px; font-weight: 600; }
  .note, .ax, .leg, .rulelab { fill: var(--muted); font-size: 10.5px; }
  .leg { font-size: 10.5px; }
  .hov:hover { fill: rgba(127,127,127,.14); }
</style>
<h1>%(head)s</h1>
<p>%(when)s · hover anywhere on the plot for every field at that instant</p>
<p>%(note)s</p>
<div class="wrap">
<svg width="%(w)d" height="%(h)d" viewBox="0 0 %(w)d %(h)d">
%(svg)s
</svg>
</div>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Plot playstick sync telemetry as a standalone HTML page.")
    ap.add_argument("file", nargs="?", metavar="FILE",
                    help="CSV from sync-log-to-csv.py, or raw journal; "
                         "stdin if omitted")
    ap.add_argument("-o", "--out", default="sync.html", metavar="FILE")
    ap.add_argument("--id", metavar="ID", help="which page load to plot")
    ap.add_argument("--start", type=float, metavar="SEC",
                    help="clip to this many seconds since the page loaded")
    ap.add_argument("--end", type=float, metavar="SEC")
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, "r", errors="replace") as fh:
            rows = load_rows(fh, args.file)
    else:
        rows = load_rows(sys.stdin, "stdin")
    key, group, groups = pick_phone(rows, args.id)
    group, x0, x1 = clip(group, args)
    with open(args.out, "w") as fh:
        fh.write(page(group, key, groups, x0, x1))
    print("%s: %d lines from %s/%s%s" % (args.out, len(group), key[0], key[1],
          (" (%d other phone(s) -- use --id)" % (len(groups) - 1))
          if len(groups) > 1 else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
