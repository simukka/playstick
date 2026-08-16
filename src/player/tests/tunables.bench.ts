import { bench, describe } from "vitest";
import { Tunables } from "../src/tunables";
import { SYNC, CLOCK, TIMING } from "../src/config";

// digest() runs on every ?debug poll to stamp the telemetry line; benched so it
// stays a short scan rather than growing per-item allocation.
describe("Tunables", () => {
  bench("digest with a couple of overrides", () => {
    const t = new Tunables({ ...SYNC }, { ...CLOCK }, { ...TIMING });
    t.apply(t.items[1]!, 200);
    t.apply(t.items[6]!, 20);
    let n = 0;
    for (let k = 0; k < 200000; k++) {
      n += t.digest().length;
    }
    if (n === 0) {
      throw new Error("unreachable");
    }
  });
});
