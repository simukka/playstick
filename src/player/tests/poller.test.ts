import { describe, it, expect } from "vitest";
import { Poller, pollDelay } from "../src/poller";
import { ApiClient } from "../src/net";
import type { PlayerState, Status } from "../src/types";

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("pollDelay", () => {
  it("is fast while something is happening, slow at rest, slowest hidden", () => {
    expect(pollDelay("playing", false)).toBe(1000);
    expect(pollDelay("preparing", false)).toBe(1000);
    expect(pollDelay("paused", false)).toBe(1000);
    expect(pollDelay("idle", false)).toBe(3000);
    expect(pollDelay("airplay", false)).toBe(3000);
    expect(pollDelay("playing", true)).toBe(5000); // a pocket wins
    expect(pollDelay("idle", true)).toBe(5000);
  });
});

interface Scheduled {
  fn: () => void;
  ms: number;
}

function harness(reply: () => { ok: boolean; body: Status }, hidden = false) {
  let state: PlayerState = "idle";
  const scheduled: Scheduled[] = [];
  const seen: Status[] = [];
  let outages = 0;
  const api = new ApiClient(() => {
    const { ok, body } = reply();
    return Promise.resolve({ ok, json: () => Promise.resolve(body) });
  });
  const poller = new Poller(api, {
    now: () => 1000,
    schedule: (fn, ms) => {
      scheduled.push({ fn, ms });
      return scheduled.length;
    },
    cancel: () => {},
    hidden: () => hidden,
    stateOf: () => state,
    onStatus: (s) => {
      state = s.state;
      seen.push(s);
    },
    onOutage: () => {
      outages++;
    },
  });
  return { poller, scheduled, seen, get outages() { return outages; } };
}

describe("Poller", () => {
  it("applies a status and schedules the next poll at the state's cadence", async () => {
    const h = harness(() => ({ ok: true, body: { state: "playing" } }));
    h.poller.poll();
    await flush();
    expect(h.seen).toHaveLength(1);
    expect(h.scheduled).toHaveLength(1);
    expect(h.scheduled[0]!.ms).toBe(1000); // now playing
  });

  it("reports an outage and still reschedules", async () => {
    const h = harness(() => {
      throw new Error("network down");
    });
    h.poller.poll();
    await flush();
    expect(h.outages).toBe(1);
    expect(h.scheduled).toHaveLength(1);
    expect(h.scheduled[0]!.ms).toBe(3000); // state stayed idle
  });

  it("never runs two chains at once", async () => {
    const h = harness(() => ({ ok: true, body: { state: "idle" } }));
    h.poller.poll();
    await flush();
    // The scheduled callback is the only way the next poll fires.
    expect(h.scheduled).toHaveLength(1);
    h.scheduled[0]!.fn();
    await flush();
    expect(h.scheduled).toHaveLength(2);
    expect(h.seen).toHaveLength(2);
  });
});
