import { describe, it, expect } from "vitest";
import { TimeDriver } from "../src/time-driver";
import { ServerClock } from "../src/clock";
import { ApiClient } from "../src/net";
import { CLOCK, TIMING } from "../src/config";

const flush = () => new Promise((r) => setTimeout(r, 0));

function harness(reply: () => { now: number; session: string }) {
  let clock = 1000;
  const api = new ApiClient(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(reply()) }),
  );
  const server = new ServerClock(CLOCK);
  let resets = 0;
  const driver = new TimeDriver(api, server, TIMING, {
    now: () => clock,
    schedule: () => {},
    onReset: () => resets++,
  });
  return {
    driver,
    server,
    advance: (dt: number) => (clock += dt),
    at: () => clock,
    get resets() {
      return resets;
    },
  };
}

describe("TimeDriver", () => {
  it("feeds a timed round trip into the clock", async () => {
    const h = harness(() => ({ now: h.at() + 12, session: "s1" }));
    await h.driver.sample();
    await flush();
    expect(h.server.hasFix()).toBe(true);
    // now() at the local instant projects to roughly local + 12.
    expect(h.server.now(h.at())! - h.at()).toBeCloseTo(12, 1);
  });

  it("notices a new daemon session and asks audio to re-place", async () => {
    let session = "s1";
    const h = harness(() => ({ now: h.at() + 3, session }));
    await h.driver.sample();
    await flush();
    expect(h.resets).toBe(1); // first session adoption
    session = "s2";
    await h.driver.sample();
    await flush();
    expect(h.resets).toBe(2);
    expect(h.server.session).toBe("s2");
  });

  it("keeps a fix across many samples without unbounded growth", async () => {
    const h = harness(() => ({ now: h.at() + 5, session: "s1" }));
    for (let k = 0; k < 50; k++) {
      await h.driver.sample();
      h.advance(2);
    }
    await flush();
    expect(h.server.hasFix()).toBe(true);
    expect(h.server.samples).toBeLessThanOrEqual(CLOCK.ratioSpan / 2 + 2);
  });
});
