import { describe, it, expect } from "vitest";
import { SyncController, type SyncElement } from "../src/sync";
import { SYNC } from "../src/config";

class FakeEl implements SyncElement {
  currentTime = 0;
  duration = 7200;
  playbackRate = 1;
}

describe("SyncController placement", () => {
  it("places the element on the first tick, then clears the pending flag", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    el.currentTime = 500;
    expect(c.seekPending).toBe(true);
    expect(c.correct(el, 100, 300, 0)).toBe("seek");
    expect(el.currentTime).toBe(300);
    expect(c.seekPending).toBe(false);
    expect(c.seeks).toBe(1);
  });

  it("hard-seeks an error past the seek threshold", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    el.currentTime = 300 + SYNC.seekLimit + 0.5;
    expect(c.correct(el, 100, 300, 0)).toBe("seek");
    expect(el.currentTime).toBe(300);
  });

  it("declines to steer with no fix or before metadata", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    expect(c.correct(el, 100, null, 0)).toBe("idle");
    const noMeta = new FakeEl();
    (noMeta as { duration: number }).duration = 0;
    expect(c.correct(noMeta, 100, 300, 0)).toBe("idle");
  });
});

describe("SyncController nudging", () => {
  it("speeds up when the element is behind and slows when ahead", () => {
    const behind = new SyncController(SYNC);
    const el = new FakeEl();
    behind.seekPending = false;
    el.currentTime = 299.9; // 100 ms behind
    expect(behind.correct(el, 1000, 300, 0)).toBe("nudge");
    expect(el.playbackRate).toBeGreaterThan(1);

    const ahead = new SyncController(SYNC);
    const el2 = new FakeEl();
    ahead.seekPending = false;
    el2.currentTime = 300.1; // 100 ms ahead
    ahead.correct(el2, 1000, 300, 0);
    expect(el2.playbackRate).toBeLessThan(1);
  });

  it("feeds the measured crystal ratio forward with no standing error", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    el.currentTime = 300;
    c.correct(el, 1000, 300, 150e-6);
    // err is 0, so the whole command is the feedforward ratio.
    expect(el.playbackRate).toBeCloseTo(1 + 150e-6, 9);
  });

  it("drops a write below the deadband", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    el.currentTime = 300 + SYNC.rateEps / SYNC.kp / 2; // error under rateEps/kp
    c.correct(el, 1000, 300, 0);
    expect(el.playbackRate).toBe(1); // nothing worth writing
    expect(c.rateWrites).toBe(0);
  });

  it("spaces writes: a second small nudge too soon is suppressed", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    el.currentTime = 299.9;
    c.correct(el, 1000, 300, 0); // writes
    expect(c.rateWrites).toBe(1);
    const rate = el.playbackRate;
    el.currentTime = 299.95;
    c.correct(el, 1000 + 1, 300, 0); // 1 s later, well under writeEvery
    expect(c.rateWrites).toBe(1); // suppressed
    expect(el.playbackRate).toBe(rate);
  });

  it("lets a large move through despite the spacing (a resume)", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    // A fresh, unsmoothed error just under the seek threshold: errF starts at
    // the raw value, so the command jumps more than rateLimit/2 in one tick,
    // which is the escape from write-spacing. This is what a resume looks like.
    el.currentTime = 300 - (SYNC.seekLimit - 0.001);
    expect(c.correct(el, 1000, 300, 0)).toBe("nudge");
    expect(c.rateWrites).toBe(1);
    expect(Math.abs(el.playbackRate - 1)).toBeGreaterThanOrEqual(SYNC.rateLimit / 2);
  });

  it("bounds how often it writes the rate over a long tracking run", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    const tick = SYNC.tick;
    let want = 300;
    let now = 1000;
    el.currentTime = want - 0.1;
    const seconds = 200;
    for (let i = 0; i < seconds / tick; i++) {
      c.correct(el, now, want, 0);
      el.currentTime += el.playbackRate * tick;
      want += tick;
      now += tick;
    }
    // Each write costs ~43 ms of audio, so bounding their frequency is the
    // whole point of writeEvery. A handful of extra early writes during
    // convergence are allowed; a write storm is not.
    expect(c.rateWrites).toBeLessThanOrEqual(seconds / SYNC.writeEvery + 6);
  });
});

describe("SyncController coast", () => {
  it("runs on the ratio alone, dropping the proportional term", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.coast(el, 1000, 120e-6);
    expect(el.playbackRate).toBeCloseTo(1 + 120e-6, 9);
  });
});

describe("SyncController closes a disturbance", () => {
  it("walks a 100 ms lag back toward zero without seeking", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    const tick = SYNC.tick;
    let want = 300;
    let now = 1000;
    el.currentTime = want - 0.1; // 100 ms behind
    const initial = Math.abs(el.currentTime - want);
    for (let i = 0; i < 800; i++) {
      c.correct(el, now, want, 0);
      // Plant: the element's own clock advances at the commanded rate; the film
      // plays at 1. A behind element commanded >1 gains on it.
      el.currentTime += el.playbackRate * tick;
      want += tick;
      now += tick;
    }
    const finalErr = Math.abs(el.currentTime - want);
    expect(finalErr).toBeLessThan(initial);
    expect(finalErr).toBeLessThan(0.03); // inside the perception threshold
    expect(c.seeks).toBe(0); // routine work is nudges, not cuts
  });
});

describe("SyncController performance", () => {
  // Backs tests/sync.bench.ts. correct() is the 4 Hz hot path for a whole film;
  // this catches a regression that put allocation or heavier math on it.
  it("holds 200k steady-state ticks under budget", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    let want = 300;
    let now = 1000;
    el.currentTime = want - 0.02;
    const t0 = performance.now();
    for (let k = 0; k < 200_000; k++) {
      c.correct(el, now, want, 60e-6);
      el.currentTime += el.playbackRate * SYNC.tick;
      want += SYNC.tick;
      now += SYNC.tick;
    }
    const ms = performance.now() - t0;
    expect(ms).toBeLessThan(200);
  });
});
