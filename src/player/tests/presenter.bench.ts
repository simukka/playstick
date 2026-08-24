import { bench, describe } from "vitest";
import { StatusPresenter } from "../src/presenter";
import { ReloadPolicy } from "../src/build";
import { makeShell } from "./harness/page";
import { asDocument } from "./harness/dom";
import type { Status } from "../src/types";

// apply() runs on every status poll (1 Hz while playing). It must stay cheap and
// churn-free: setText and toggleClass already skip no-op writes, and the view
// switcher repaints only on a real change. This guards that per-poll path.
describe("StatusPresenter.apply", () => {
  bench("steady playing poll", () => {
    const shell = makeShell();
    const reload = new ReloadPolicy({ reload: () => {} }, "here");
    let clock = 1000;
    const p = new StatusPresenter(asDocument(shell.doc), {
      reload,
      stamp: (path) => path,
      now: () => clock,
      ...shell.parts,
    });
    const base: Status = {
      state: "playing",
      title: "Dune",
      duration: 7200,
      position: 0,
      audio: true,
    };
    for (let k = 0; k < 50000; k++) {
      clock += 1000;
      p.apply({ ...base, position: k % 7200 });
    }
  });
});
