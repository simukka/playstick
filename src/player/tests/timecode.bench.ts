import { bench, describe } from "vitest";
import { ServerClock } from "../src/clock";
import { FilmClock, targetFor } from "../src/timecode";
import { CLOCK } from "../src/config";

function fixed(): FilmClock {
  const c = new ServerClock(CLOCK);
  c.adoptSession("s1");
  for (let k = 0; k < 60; k++) {
    c.accept(5 + 50e-6 * k * 2, 0.003, 1000 + k * 2);
  }
  const f = new FilmClock(c);
  f.set({ tc: 100, at: 1120, rate: 1, epoch: 1 });
  return f;
}

// now() + targetFor() run on every correction tick alongside the controller, so
// they are on the same 4 Hz hot path.
describe("FilmClock projection", () => {
  bench("now() + targetFor()", () => {
    const f = fixed();
    let acc = 0;
    for (let k = 0; k < 200000; k++) {
      acc += targetFor(f.now(1120 + k * 0.001), 1.5, -30)!;
    }
    if (acc === 0) {
      throw new Error("unreachable; keeps the loop from being optimised away");
    }
  });
});
