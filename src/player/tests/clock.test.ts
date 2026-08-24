import { describe, it, expect } from "vitest";
import { ServerClock } from "../src/clock";
import { CLOCK } from "../src/config";

// A quiet-wire sample: the daemon's clock leads this phone's by `offset`, the
// exchange took `rtt`, and it landed at local time `at`. `off` as the clock
// stores it is the daemon time minus the local midpoint, which for a symmetric
// round trip measured this way is exactly the offset.
function feed(
  c: ServerClock,
  offset: number,
  rtt: number,
  at: number,
  session = "s1",
): void {
  c.adoptSession(session);
  c.accept(offset, rtt, at);
}

describe("ServerClock offset", () => {
  it("has no fix until a sample arrives", () => {
    const c = new ServerClock(CLOCK);
    expect(c.hasFix()).toBe(false);
    expect(c.now(1000)).toBeNull();
  });

  it("projects the local clock into the daemon's", () => {
    const c = new ServerClock(CLOCK);
    feed(c, 12.5, 0.004, 1000);
    expect(c.hasFix()).toBe(true);
    // No ratio yet (one sample), so it is a flat offset.
    expect(c.now(1000)).toBeCloseTo(1012.5, 9);
    expect(c.now(1001)).toBeCloseTo(1013.5, 9);
  });

  it("keeps the quickest recent exchange as the offset", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    c.accept(10.0, 0.05, 1000); // slow
    c.accept(10.02, 0.004, 1000.1); // quick and right
    c.accept(10.2, 0.2, 1000.2); // slowest, furthest off
    // The 4 ms exchange wins over the 50 ms and 200 ms ones.
    expect(c.bestRtt()).toBeCloseTo(0.004, 9);
    expect(c.now(1000.1)).toBeCloseTo(1010.12, 6);
  });

  it("drops the fix once every sample has aged past offMaxAge", () => {
    const c = new ServerClock(CLOCK);
    feed(c, 5, 0.004, 1000);
    expect(c.hasFix()).toBe(true);
    c.tick(1000 + CLOCK.offMaxAge + 1);
    expect(c.hasFix()).toBe(false);
    expect(c.now(1031)).toBeNull();
  });
});

describe("ServerClock ratio", () => {
  it("fits the crystal slope from a drifting offset", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    // 50 ppm: the daemon gains 50 microseconds a second on this phone. Over the
    // window that is a straight line the least-squares fit must recover.
    const ppm = 50e-6;
    for (let k = 0; k <= 60; k++) {
      const at = 1000 + k * 2; // a sample every 2 s for 120 s
      c.accept(3 + ppm * (at - 1000), 0.003, at);
    }
    expect(c.ratio).toBeCloseTo(ppm, 8);
  });

  it("carries the offset forward at the fitted ratio", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    const ppm = 100e-6;
    for (let k = 0; k <= 60; k++) {
      const at = 1000 + k * 2;
      c.accept(2 + ppm * (at - 1000), 0.003, at);
    }
    const at = 1120;
    // now() = local + best.off + ratio*(local - best.at). The projection must
    // beat treating the offset as flat: 20 s at 100 ppm is 2 ms.
    const flat = c.now(at + 20)! - c.now(at)!;
    expect(flat).toBeCloseTo(20 * (1 + ppm), 6);
  });

  it("ignores samples dirtier than twice the best round trip", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    // A clean ramp, plus one wildly late sample carrying a bogus offset. The
    // slope must come from the clean ones only.
    const ppm = 40e-6;
    for (let k = 0; k <= 60; k++) {
      const at = 1000 + k * 2;
      c.accept(1 + ppm * (at - 1000), 0.003, at);
    }
    c.accept(1 + ppm * 60 + 5, 0.5, 1121); // 500 ms round trip, 5 s of nonsense
    c.tick(1121);
    expect(c.ratio).toBeCloseTo(ppm, 7);
  });

  it("holds the last honest slope when the span is too short", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    c.accept(1, 0.003, 2000);
    c.accept(1, 0.003, 2001);
    // Two samples over 1 s: below ratioMin, so no slope is claimed.
    expect(c.ratio).toBe(0);
  });

  it("resets everything on a new daemon session", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    const ppm = 30e-6;
    for (let k = 0; k <= 60; k++) {
      const at = 1000 + k * 2;
      c.accept(1 + ppm * (at - 1000), 0.003, at);
    }
    expect(c.ratio).not.toBe(0);
    expect(c.adoptSession("s2")).toBe(true);
    expect(c.ratio).toBe(0);
    expect(c.hasFix()).toBe(false);
    expect(c.samples).toBe(0);
  });
});

describe("ServerClock is allocation-stable under a long run", () => {
  it("bounds the window to ratioSpan however many samples arrive", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    // An hour of samples at 5 Hz into a clock whose window is 180 s: the ring
    // must never exceed the window, which is what keeps it from growing.
    for (let k = 0; k < 18000; k++) {
      const at = 1000 + k * 0.2;
      c.accept(1, 0.003, at);
    }
    expect(c.samples).toBeLessThanOrEqual(CLOCK.ratioSpan / 0.2 + 2);
    expect(c.hasFix()).toBe(true);
  });

  // The performance assertion that backs tests/clock.bench.ts. Budgets are
  // deliberately loose -- 20x the measured time in the container -- so this
  // catches a pathological regression (per-sample allocation, an O(n^2) prune)
  // without flaking on a busy CI box.
  it("holds a full-window accept() under budget", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    for (let k = 0; k < 900; k++) {
      c.accept(1 + 50e-6 * k * 0.2, 0.003, 1000 + k * 0.2);
    }
    let at = 1000 + 900 * 0.2;
    const t0 = performance.now();
    for (let k = 0; k < 20000; k++) {
      at += 0.2;
      c.accept(1 + 50e-6 * (at - 1000), 0.003, at);
    }
    const ms = performance.now() - t0;
    expect(ms).toBeLessThan(400);
  });

  it("holds a million now() projections under budget", () => {
    const c = new ServerClock(CLOCK);
    c.adoptSession("s1");
    for (let k = 0; k < 900; k++) {
      c.accept(1 + 50e-6 * k * 0.2, 0.003, 1000 + k * 0.2);
    }
    let acc = 0;
    const t0 = performance.now();
    for (let k = 0; k < 1_000_000; k++) {
      acc += c.now(1180 + k * 0.001)!;
    }
    const ms = performance.now() - t0;
    expect(acc).not.toBe(0);
    expect(ms).toBeLessThan(200);
  });
});
