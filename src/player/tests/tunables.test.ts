import { describe, it, expect } from "vitest";
import { Tunables, type StorageLike } from "../src/tunables";
import { SYNC, CLOCK, TIMING } from "../src/config";

// Fresh copies per test: the tunables mutate the config objects in place, which
// is the whole point on the device but must not leak between tests here.
function fresh() {
  return {
    sync: { ...SYNC },
    clock: { ...CLOCK },
    timing: { ...TIMING },
  };
}

class Mem implements StorageLike {
  d: Record<string, string> = {};
  getItem(k: string) {
    return k in this.d ? this.d[k]! : null;
  }
  setItem(k: string, v: string) {
    this.d[k] = v;
  }
  removeItem(k: string) {
    delete this.d[k];
  }
}

const byCode = (t: Tunables, code: string) => t.items.find((x) => x.code === code)!;

describe("Tunables display", () => {
  it("shows internal fractions and seconds in ppm and ms", () => {
    const c = fresh();
    const t = new Tunables(c.sync, c.clock, c.timing);
    expect(t.text(byCode(t, "kp"))).toBe("150 ppm/ms"); // 0.15 * 1000
    expect(t.text(byCode(t, "sl"))).toBe("250 ms"); // 0.25 * 1000
    expect(t.text(byCode(t, "rl"))).toBe("20000 ppm"); // 0.02 * 1e6
  });
});

describe("Tunables editing", () => {
  it("clamps and snaps to the step grid, and writes through to config", () => {
    const c = fresh();
    const t = new Tunables(c.sync, c.clock, c.timing);
    const kp = byCode(t, "kp");
    t.apply(kp, 99999); // way over max 400
    expect(t.value(kp)).toBe(400);
    expect(c.sync.kp).toBeCloseTo(0.4, 9);
    t.apply(kp, 152); // snaps to nearest step of 5 -> 150
    expect(t.value(kp)).toBe(150);
  });

  it("steps up and down", () => {
    const c = fresh();
    const t = new Tunables(c.sync, c.clock, c.timing);
    const we = byCode(t, "we"); // writeEvery, step 1 s, default 15
    t.step(we, 1);
    expect(t.value(we)).toBe(16);
    t.step(we, -1);
    expect(t.value(we)).toBe(15);
  });

  it("re-arms the tick when the correction interval changes", () => {
    const c = fresh();
    let rearms = 0;
    const t = new Tunables(c.sync, c.clock, c.timing, () => rearms++);
    t.step(byCode(t, "tk"), 1);
    expect(rearms).toBe(1);
  });
});

describe("Tunables persistence and digest", () => {
  it("is empty and unchanged on a stock build", () => {
    const c = fresh();
    const t = new Tunables(c.sync, c.clock, c.timing);
    expect(t.digest()).toBe("");
    expect(t.items.every((x) => !t.changed(x))).toBe(true);
  });

  it("records only the changed values in the digest", () => {
    const c = fresh();
    const t = new Tunables(c.sync, c.clock, c.timing);
    t.apply(byCode(t, "kp"), 200);
    t.apply(byCode(t, "we"), 20);
    expect(t.digest()).toBe("kp:200,we:20");
  });

  it("saves only overrides and reloads them under ?debug", () => {
    const c = fresh();
    const store = new Mem();
    const t = new Tunables(c.sync, c.clock, c.timing);
    t.apply(byCode(t, "kp"), 200);
    t.save(store);
    expect(JSON.parse(store.d["ps.snd.tune"]!)).toEqual({ kp: 200 });

    const c2 = fresh();
    const t2 = new Tunables(c2.sync, c2.clock, c2.timing);
    t2.load(store, false); // not ?debug: ignored
    expect(t2.value(byCode(t2, "kp"))).toBe(150);
    t2.load(store, true);
    expect(t2.value(byCode(t2, "kp"))).toBe(200);
  });

  it("clears the store when nothing is overridden", () => {
    const c = fresh();
    const store = new Mem();
    store.d["ps.snd.tune"] = '{"kp":200}';
    const t = new Tunables(c.sync, c.clock, c.timing);
    t.save(store); // nothing changed
    expect(store.d["ps.snd.tune"]).toBeUndefined();
  });

  it("resets everything to the shipped values", () => {
    const c = fresh();
    const t = new Tunables(c.sync, c.clock, c.timing);
    t.apply(byCode(t, "kp"), 300);
    t.reset();
    expect(t.value(byCode(t, "kp"))).toBe(150);
    expect(c.sync.kp).toBeCloseTo(0.15, 9);
  });
});
