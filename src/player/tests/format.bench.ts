import { bench, describe } from "vitest";
import { timeLeft, barWidth } from "../src/format";

// Called on every playing poll to repaint the countdown and the bar. Cheap, but
// on the per-poll path, so it stays benched against accidental string churn.
describe("format", () => {
  bench("timeLeft + barWidth", () => {
    let n = 0;
    for (let k = 0; k < 200000; k++) {
      n += timeLeft(k % 7200).length + barWidth(k % 7200, 7200).length;
    }
    if (n === 0) {
      throw new Error("unreachable");
    }
  });
});
