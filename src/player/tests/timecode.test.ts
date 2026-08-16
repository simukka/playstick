import { describe, it, expect } from "vitest";
import { ServerClock } from "../src/clock";
import { FilmClock, targetFor } from "../src/timecode";
import { CLOCK } from "../src/config";

// A clock with a flat, known offset: the daemon leads this phone by `off`.
function clockAt(off: number): ServerClock {
  const c = new ServerClock(CLOCK);
  c.adoptSession("s1");
  c.accept(off, 0.003, 1000);
  return c;
}

describe("FilmClock", () => {
  it("is null until both a clock fix and a timeline exist", () => {
    const c = new ServerClock(CLOCK);
    const f = new FilmClock(c);
    expect(f.now(1000)).toBeNull(); // no fix, no timeline
    c.adoptSession("s1");
    c.accept(5, 0.003, 1000);
    expect(f.now(1000)).toBeNull(); // fix but no timeline
    f.set({ tc: 100, at: 1005, rate: 1, epoch: 1 });
    expect(f.now(1000)).not.toBeNull();
  });

  it("evaluates the timeline at the daemon-clock instant", () => {
    const c = clockAt(5); // daemon = local + 5
    const f = new FilmClock(c);
    // At daemon time 1005 the film was at 100 and playing. srvNow(1000) = 1005,
    // so the film is at 100 right now.
    f.set({ tc: 100, at: 1005, rate: 1, epoch: 1 });
    expect(f.now(1000)).toBeCloseTo(100, 9);
    // Two local seconds later the film has advanced two seconds.
    expect(f.now(1002)).toBeCloseTo(102, 9);
  });

  it("freezes on a paused timeline", () => {
    const c = clockAt(0);
    const f = new FilmClock(c);
    f.set({ tc: 50, at: 1000, rate: 0, epoch: 7 });
    expect(f.now(1000)).toBeCloseTo(50, 9);
    expect(f.now(1010)).toBeCloseTo(50, 9); // rate 0: no advance
  });

  it("reports an epoch change so the caller can re-place the element", () => {
    const c = clockAt(0);
    const f = new FilmClock(c);
    expect(f.set({ tc: 0, at: 1000, rate: 1, epoch: 1 })).toBe(true); // first
    expect(f.set({ tc: 5, at: 1005, rate: 1, epoch: 1 })).toBe(false); // same line
    expect(f.set({ tc: 0, at: 1010, rate: 1, epoch: 2 })).toBe(true); // a seek
    expect(f.epoch).toBe(2);
    expect(f.set(null)).toBe(false);
    expect(f.epoch).toBeNull();
  });
});

describe("targetFor", () => {
  it("adds the track origin and this device's trim", () => {
    expect(targetFor(100, 0, 0)).toBeCloseTo(100, 9);
    expect(targetFor(100, 1.5, 0)).toBeCloseTo(101.5, 9);
    expect(targetFor(100, 0, 200)).toBeCloseTo(100.2, 9); // +200 ms
    expect(targetFor(100, -0.5, -30)).toBeCloseTo(99.47, 9);
  });

  it("stays null with no film position", () => {
    expect(targetFor(null, 1, 100)).toBeNull();
  });
});
