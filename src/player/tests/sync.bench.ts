import { bench, describe } from "vitest";
import { SyncController, type SyncElement } from "../src/sync";
import { SYNC } from "../src/config";

class FakeEl implements SyncElement {
  currentTime = 0;
  duration = 7200;
  playbackRate = 1;
}

// correct() runs at TICK (4 Hz) for the length of a film, on a phone. The
// steady-state cost is what matters: a nudge with the write gates in play. The
// budget assertion is in sync.test.ts.
describe("SyncController.correct", () => {
  bench("nudge, steady state", () => {
    const c = new SyncController(SYNC);
    const el = new FakeEl();
    c.seekPending = false;
    let want = 300;
    let now = 1000;
    el.currentTime = want - 0.02;
    for (let k = 0; k < 100000; k++) {
      c.correct(el, now, want, 60e-6);
      el.currentTime += el.playbackRate * SYNC.tick;
      want += SYNC.tick;
      now += SYNC.tick;
    }
  });
});
