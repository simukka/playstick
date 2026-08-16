import { bench, describe } from "vitest";
import { pollDelay } from "../src/poller";
import type { PlayerState } from "../src/types";

// pollDelay is the only CPU-bearing part of the poll loop (the rest is I/O and
// scheduling); benched to keep it a branch table rather than growing work.
const states: PlayerState[] = ["idle", "playing", "paused", "preparing", "airplay"];

describe("pollDelay", () => {
  bench("decision", () => {
    let n = 0;
    for (let k = 0; k < 1_000_000; k++) {
      n += pollDelay(states[k % states.length]!, (k & 1) === 0);
    }
    if (n === 0) {
      throw new Error("unreachable");
    }
  });
});
