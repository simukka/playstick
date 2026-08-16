import { bench, describe } from "vitest";
import { Telemetry, type TelemetrySnapshot } from "../src/telemetry";

const s: TelemetrySnapshot = {
  now: 100, listening: true, hasSrc: true, paused: false, hidden: false,
  currentTime: 100, errMs: 5, ratePpm: 150, ratioPpm: 60, driftPpm: 10,
  offsetMs: 12.5, rttMs: 3, samples: 40, epoch: 7,
  writes: 0, seeks: 0, waits: 0, buffering: 0, digest: "",
};

// build() runs once per poll while ?debug is on; benched to keep the string
// assembly off the list of things that could perturb what it measures.
describe("Telemetry.build", () => {
  bench("per-poll line", () => {
    const t = new Telemetry(() => "x");
    for (let k = 0; k < 100000; k++) {
      t.build({ ...s, writes: k, currentTime: k % 7200 });
    }
  });
});
