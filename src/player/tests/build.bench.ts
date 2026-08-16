import { bench, describe } from "vitest";
import { ReloadPolicy } from "../src/build";

// consider() runs on every status poll, so it must stay a handful of branches
// with no allocation. The benchmark guards against it quietly growing work --
// a fetch, a DOM read -- on that per-poll path.
describe("ReloadPolicy.consider", () => {
  bench("steady state, build matches", () => {
    const p = new ReloadPolicy({ reload: () => {} }, "here");
    for (let k = 0; k < 100000; k++) {
      p.consider("here", "playing");
    }
  });
});
