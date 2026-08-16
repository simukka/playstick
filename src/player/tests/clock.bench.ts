import { bench, describe } from "vitest";
import { ServerClock } from "../src/clock";
import { CLOCK } from "../src/config";

// The clock is touched on every /api/time reply and every correction tick, on a
// phone, for the length of a film. These benchmarks track the two hot paths so a
// change that reintroduces per-sample allocation or a rebuild-the-window prune
// shows up as a throughput drop. The matching budget assertions live in
// clock.test.ts so a regression fails the suite, not just the report.

function primed(): ServerClock {
  const c = new ServerClock(CLOCK);
  c.adoptSession("s1");
  // Fill the window so accept() below does real prune + pick + fit work.
  for (let k = 0; k < 900; k++) {
    c.accept(1 + 50e-6 * k * 0.2, 0.003, 1000 + k * 0.2);
  }
  return c;
}

describe("ServerClock hot paths", () => {
  bench("accept() in steady state (full 180 s window)", () => {
    const c = primed();
    let at = 1000 + 900 * 0.2;
    for (let k = 0; k < 1000; k++) {
      at += 0.2;
      c.accept(1 + 50e-6 * (at - 1000), 0.003, at);
    }
  });

  bench("now() projection", () => {
    const c = primed();
    let acc = 0;
    for (let k = 0; k < 100000; k++) {
      acc += c.now(1180 + k * 0.001)!;
    }
    if (acc === 0) {
      throw new Error("unreachable; keeps the loop from being optimised away");
    }
  });

  bench("tick() re-pick without a new sample", () => {
    const c = primed();
    for (let k = 0; k < 1000; k++) {
      c.tick(1180 + k * 0.25);
    }
  });
});
