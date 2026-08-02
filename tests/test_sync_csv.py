"""Tests for scripts/sync-log-to-csv.py.

The script turns a phone's telemetry into the table that conclusions get drawn
from, which is what makes it worth testing: a field silently landing in the
wrong column, or a truncated line quietly becoming a row of plausible numbers,
would not look like a failure. It would look like an answer.

Independent of tests/support.py -- nothing here needs the daemon, its
environment, or a socket.
"""

import importlib.util
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "sync-log-to-csv.py")

# The hyphen in the filename keeps it out of `import`, which is the right name
# for a command and the wrong one for a module.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("sync_log_to_csv", SCRIPT)
csvtool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csvtool)


# A real line off the device, as journalctl's default format renders it.
LINE = (
    "Aug 02 07:35:11 simukka-atom playstick-web.py[60875]: playstick: sync "
    "10.0.1.237 playing pos=64.29 buf=0 v=1;id=d32b8e;t=87.1;st=play;hid=0;"
    "ct=64.08;rs=4;nb=1;ahead=286.7;amin=286.8;err=-195;errp=-195;rate=19925;"
    "drift=-427;step=0.0;ns=8;rtt=23;trim=0;w=2;dw=445;sk=0;wt=0;bf=0;lag=43;"
    "ls=2")


def blob(**fields):
    """A telemetry line with the given fields, as -o cat renders it."""
    pairs = ";".join("%s=%s" % kv for kv in fields.items())
    return ("playstick: sync 10.0.1.5 playing pos=1.00 buf=0 v=1;" + pairs)


class Parsing(unittest.TestCase):
    def test_a_real_line_lands_in_the_right_columns(self):
        row = csvtool.parse(LINE)
        # The year is not asserted: this format does not carry one, so it is
        # inferred from the clock, and pinning it here would make the suite
        # fail on New Year's Day. Timestamps has the test that covers it.
        self.assertTrue(row["time"].endswith("-08-02T07:35:11.000"), row["time"])
        self.assertEqual(row["host"], "simukka-atom")
        self.assertEqual(row["ip"], "10.0.1.237")
        self.assertEqual(row["state"], "playing")     # the daemon's view
        self.assertEqual(row["st"], "play")           # the phone's
        self.assertEqual(row["pos"], "64.29")
        self.assertEqual(row["ct"], "64.08")
        self.assertEqual(row["ahead"], "286.7")
        self.assertEqual(row["lag"], "43")
        self.assertEqual(row["ls"], "2")

    def test_every_field_the_page_sends_has_a_column(self):
        row = csvtool.parse(LINE)
        missing = set(row) - set(csvtool.COLUMNS)
        self.assertEqual(missing, set(),
                         "no column for %s; add it to COLUMNS" % missing)

    def test_lines_that_are_not_telemetry_are_ignored(self):
        for line in ("playstick: started, library=/srv/movies",
                     "Aug 02 07:35:09 host kernel: something else entirely",
                     "", "   ", "playstick: sync"):
            self.assertIsNone(csvtool.parse(line), line)

    def test_an_unknown_position_becomes_an_empty_cell(self):
        # The daemon writes pos=? before mpv has reported one. A literal "?"
        # in a numeric column would poison anything that read the file.
        row = csvtool.parse(
            "playstick: sync 10.0.1.5 stopped pos=? buf=0 v=1;id=ab;st=off")
        self.assertEqual(row["pos"], "")

    def test_a_field_truncated_mid_pair_is_dropped(self):
        # The daemon cuts the header at 400 characters, which can land inside a
        # pair. Half a value is worse than no value.
        row = csvtool.parse(blob(id="ab", t="3.0", st="play", ct="1.2") + ";la")
        self.assertNotIn("la", row)
        self.assertEqual(row["ct"], "1.2")

    def test_a_blob_field_cannot_overwrite_the_daemons_own(self):
        # The blob comes from an unauthenticated client. It is filtered, but
        # `pos=9;ip=9` survives that filter intact, and the daemon's account of
        # where the film was is the one that must win.
        row = csvtool.parse(blob(pos="9", ip="9.9.9.9", state="fake", dt="99"))
        self.assertEqual(row["pos"], "1.00")
        self.assertEqual(row["ip"], "10.0.1.5")
        self.assertEqual(row["state"], "playing")
        self.assertNotIn("dt", row)

    def test_a_field_this_script_has_never_heard_of_is_kept(self):
        row = csvtool.parse(blob(id="ab", t="1", newthing="7"))
        self.assertEqual(row["newthing"], "7")


class Timestamps(unittest.TestCase):
    def test_short_iso_keeps_its_offset(self):
        row = csvtool.parse(
            "2026-08-02T07:35:11+0300 atom playstick-web.py[1]: " +
            blob(id="ab", t="1"))
        self.assertEqual(row["time"], "2026-08-02T07:35:11.000+03:00")

    def test_json_carries_an_exact_timestamp(self):
        row = csvtool.parse(
            '{"__REALTIME_TIMESTAMP":"1785000911000000",'
            '"_HOSTNAME":"atom","MESSAGE":"%s"}' % blob(id="ab", t="1"))
        self.assertTrue(row["time"].startswith("20"))
        self.assertEqual(row["host"], "atom")

    def test_cat_output_has_no_timestamp_and_that_is_allowed(self):
        # -o cat is what the README's capture recipe used first; the phone's
        # own `t` clock still carries the whole analysis.
        row = csvtool.parse(blob(id="ab", t="1"))
        self.assertEqual(row["time"], "")
        self.assertEqual(row["host"], "")

    def test_a_december_line_read_in_january_is_not_dated_next_year(self):
        from datetime import datetime
        now = datetime(2026, 1, 3, 10, 0, 0)
        self.assertEqual(csvtool.stamp_syslog("Dec 28 22:14:03", now).year,
                         2025)
        self.assertEqual(csvtool.stamp_syslog("Jan 02 22:14:03", now).year,
                         2026)


class Derived(unittest.TestCase):
    def rows(self, *specs):
        rows = [csvtool.parse(blob(**s)) for s in specs]
        return csvtool.derive(rows)

    def test_dt_is_the_interval_the_counters_describe(self):
        a, b = self.rows({"id": "ab", "t": "10.0"}, {"id": "ab", "t": "11.0"})
        self.assertEqual(a.get("dt", ""), "")     # nothing to measure against
        self.assertEqual(b["dt"], "1.0")
        self.assertEqual(b["gap"], "0")

    def test_a_skipped_poll_is_flagged(self):
        # The page backs off to 5 s with the screen locked, and iOS throttles
        # further. A line covering six seconds must not be read as one.
        _, b = self.rows({"id": "ab", "t": "10.0"}, {"id": "ab", "t": "16.1"})
        self.assertEqual(b["dt"], "6.1")
        self.assertEqual(b["gap"], "1")

    def test_two_phones_do_not_share_a_series(self):
        rows = csvtool.derive([
            csvtool.parse("playstick: sync 10.0.1.5 playing pos=1 buf=0 "
                          "v=1;id=aa;t=50.0"),
            csvtool.parse("playstick: sync 10.0.1.6 playing pos=1 buf=0 "
                          "v=1;id=bb;t=2.0"),
        ])
        self.assertEqual(rows[1].get("dt", ""), "")

    def test_a_page_reload_restarts_the_clock_without_a_negative_dt(self):
        rows = self.rows({"id": "aa", "t": "300.0"}, {"id": "bb", "t": "0.5"})
        self.assertEqual(rows[1].get("dt", ""), "")

    def test_ctpos_is_the_two_clocks_against_each_other(self):
        row = csvtool.parse(
            "playstick: sync 10.0.1.5 playing pos=64.29 buf=0 "
            "v=1;id=ab;t=1;ct=64.08")
        csvtool.derive([row])
        self.assertEqual(row["ctpos"], "-210")

    def test_a_line_without_both_clocks_gets_no_ctpos(self):
        row = csvtool.parse(
            "playstick: sync 10.0.1.5 stopped pos=? buf=0 v=1;id=ab;st=off")
        csvtool.derive([row])
        self.assertEqual(row.get("ctpos", ""), "")

    def test_unparseable_numbers_do_not_take_the_run_down(self):
        rows = self.rows({"id": "ab", "t": "x"}, {"id": "ab", "t": "y"})
        self.assertEqual(rows[1].get("dt", ""), "")


class CommandLine(unittest.TestCase):
    def run_main(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr, sys.stdin
        sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO(stdin)
        try:
            code = csvtool.main(argv)
        finally:
            sys.stdout, sys.stderr, sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def test_a_journal_on_stdin_becomes_a_csv(self):
        code, out, _ = self.run_main([], stdin=LINE + "\n")
        self.assertEqual(code, 0)
        head, row = out.splitlines()[:2]
        self.assertEqual(head.split(",")[:5],
                         ["time", "host", "ip", "id", "t"])
        self.assertEqual(row.split(",")[2], "10.0.1.237")

    def test_out_of_order_input_is_sorted_before_dt_is_taken(self):
        # journalctl -f into a file is ordered; two files concatenated are not,
        # and a negative dt would silently corrupt every rate derived from it.
        late = blob(id="ab", t="20.0")
        early = blob(id="ab", t="19.0")
        _, out, _ = self.run_main([], stdin=late + "\n" + early + "\n")
        dt = [line.split(",")[5] for line in out.splitlines()[1:]]
        self.assertEqual(dt, ["", "1.0"])

    def test_the_id_filter_keeps_one_page_load(self):
        two = LINE + "\n" + blob(id="other", t="1.0", st="play")
        _, out, _ = self.run_main(["--id", "d32b8e"], stdin=two)
        self.assertEqual(len(out.splitlines()), 2)

    def test_the_playing_filter_drops_idle_phones(self):
        two = LINE + "\n" + blob(id="ab", t="1.0", st="idle")
        _, out, _ = self.run_main(["--playing"], stdin=two)
        self.assertEqual(len(out.splitlines()), 2)

    def test_an_empty_capture_says_so_and_fails(self):
        # The likeliest outcome of a first attempt is a phone that was never
        # listening. Exiting 0 with a header-only file hides that until the
        # film is over and the projector is off.
        code, out, err = self.run_main([], stdin="playstick: started\n")
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("?debug", err)

    def test_the_summary_goes_to_stderr_and_not_into_the_csv(self):
        code, out, err = self.run_main(["--summary"], stdin=LINE + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("10.0.1.237/d32b8e", err)
        self.assertIn("1 telemetry lines from 1 phone", err)

    def test_the_summary_reports_the_worst_of_each_thing(self):
        rows = "\n".join([
            blob(id="ab", t="1", st="play", lag="19", ls="0", amin="200.0",
                 w="1"),
            blob(id="ab", t="2", st="play", lag="310", ls="4", amin="0.4",
                 w="5"),
        ])
        _, _, err = self.run_main(["--summary"], stdin=rows)
        self.assertIn("310 ms", err)      # the peak, not the mean
        self.assertIn("0.4s", err)        # the low-water mark
        self.assertIn(" 4 ", " %s " % err.split("\n")[2])  # stalls, summed

    def test_a_phone_that_never_played_reports_no_buffer_rather_than_zero(self):
        _, _, err = self.run_main(["--summary"], stdin=blob(id="ab", st="off"))
        self.assertNotIn("0.0s", err)

    def test_a_newer_page_than_this_script_is_flagged_not_dropped(self):
        _, out, err = self.run_main(["--summary"],
                                    stdin=blob(id="ab", t="1", quux="7"))
        self.assertIn("quux", out.splitlines()[0])
        self.assertIn("newer fields", err)


if __name__ == "__main__":
    unittest.main()
