// The skip ramp and the queue that turns presses into requests.
//
// Both halves matter for different reasons. The ramp is a promise made in the
// UI -- hold for three seconds, go half an hour -- and a constant nudged for
// feel would break it silently. The queue is what keeps a child tapping a
// button from being a seek per tap on a box reading the film over CIFS.
import { describe, it, expect } from "vitest";
import { SeekPad, jumpFor, STEP, FLUSH, TICK } from "../src/seek";

function pad() {
  let clock = 0;
  const timers = new Map<number, { fn: () => void; at: number }>();
  let next = 1;
  const sent: number[] = [];
  const seen: number[] = [];
  const p = new SeekPad({
    now: () => clock,
    schedule: (fn, ms) => {
      const id = next++;
      timers.set(id, { fn, at: clock + ms });
      return id;
    },
    cancel: (id) => void timers.delete(id),
    onChange: (v) => seen.push(v),
    send: (d) => sent.push(d),
  });
  const advance = (ms: number) => {
    const end = clock + ms;
    for (let guard = 0; guard < 1000; guard++) {
      const due = [...timers.entries()]
        .filter(([, t]) => t.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) {
        break;
      }
      timers.delete(due[0]);
      clock = due[1].at;
      due[1].fn();
    }
    clock = end;
  };
  return { p, sent, seen, advance };
}

describe("jumpFor", () => {
  it("a press with no hold is ten seconds", () => {
    expect(jumpFor(0)).toBe(STEP);
  });

  it("three seconds is half an hour -- the stated contract", () => {
    expect(jumpFor(3000)).toBe(1800);
  });

  it("climbs smoothly in between", () => {
    // Roughly a doubling every 400 ms, and monotone throughout.
    expect(jumpFor(400)).toBe(20);
    expect(jumpFor(1000)).toBe(55);
    let last = 0;
    for (let t = 0; t <= 3000; t += 50) {
      const v = jumpFor(t);
      expect(v).toBeGreaterThanOrEqual(last);
      last = v;
    }
  });

  it("rounds to something a person can read", () => {
    for (let t = 0; t <= 3000; t += 17) {
      const v = jumpFor(t);
      const step = v < 60 ? 5 : v < 600 ? 30 : 60;
      expect(v % step).toBe(0);
    }
  });

  it("stops climbing rather than reading a day and a half", () => {
    expect(jumpFor(60_000)).toBe(4 * 3600);
  });
});

describe("SeekPad", () => {
  it("sends one press, once, after the flush", () => {
    const { p, sent, advance } = pad();
    p.press(1);
    p.release();
    expect(sent).toEqual([]);
    advance(FLUSH + 1);
    expect(sent).toEqual([STEP]);
    expect(p.pending).toBe(0);
  });

  it("collects repeated taps into one request", () => {
    const { p, sent, advance } = pad();
    for (let i = 0; i < 4; i++) {
      p.press(1);
      p.release();
      advance(80);
    }
    expect(sent).toEqual([]);
    advance(FLUSH + 1);
    expect(sent).toEqual([40]);
  });

  it("taps the other way subtract, and nothing is sent when they cancel out", () => {
    const { p, sent, advance } = pad();
    p.press(1);
    p.release();
    advance(50);
    p.press(-1);
    p.release();
    expect(p.pending).toBe(0);
    advance(FLUSH + 1);
    expect(sent).toEqual([]);
  });

  it("previews the hold as it grows, then sends the total once", () => {
    const { p, sent, seen, advance } = pad();
    p.press(1);
    expect(p.pending).toBe(STEP);
    advance(TICK * 4);
    expect(p.pending).toBeGreaterThan(STEP);
    advance(3000 - TICK * 4);
    p.release();
    expect(p.pending).toBe(1800);
    // It was drawn on the way, not only at the end.
    expect(seen.length).toBeGreaterThan(5);
    advance(FLUSH + 1);
    expect(sent).toEqual([1800]);
  });

  it("a hold continues from taps already queued", () => {
    const { p, sent, advance } = pad();
    p.press(1);
    p.release();
    advance(50);
    p.press(1);
    advance(1000);
    p.release();
    expect(p.pending).toBe(STEP + jumpFor(1000));
    advance(FLUSH + 1);
    expect(sent).toEqual([STEP + jumpFor(1000)]);
  });

  it("an aborted hold gives back everything it grew", () => {
    const { p, sent, advance } = pad();
    p.press(1);
    p.release();
    advance(50);
    p.press(1);
    advance(2500);
    p.abort();
    expect(p.pending).toBe(STEP); // the tap survives; the hold does not
    advance(FLUSH + 1);
    expect(sent).toEqual([STEP]);
  });

  it("an aborted hold with nothing queued sends nothing at all", () => {
    const { p, sent, advance } = pad();
    p.press(-1);
    advance(2000);
    p.abort();
    advance(FLUSH * 4);
    expect(sent).toEqual([]);
  });

  it("stops ticking once released", () => {
    const { p, advance } = pad();
    p.press(1);
    advance(500);
    p.release();
    const settled = p.pending;
    advance(2000);
    // The flush has fired by now, so the only thing that could have changed
    // this is a tick that outlived the hold.
    expect(p.pending).toBe(0);
    expect(settled).toBe(jumpFor(500));
  });

  it("a second finger on the other button does not restart the ramp", () => {
    const { p, advance } = pad();
    p.press(1);
    advance(1000);
    p.press(-1);
    advance(1000);
    p.release();
    expect(p.pending).toBe(jumpFor(2000));
  });

  it("clear() drops a queued skip without sending it", () => {
    const { p, sent, advance } = pad();
    p.press(1);
    p.release();
    p.clear();
    advance(FLUSH * 4);
    expect(sent).toEqual([]);
    expect(p.pending).toBe(0);
  });
});
