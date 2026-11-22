// The calibration wizard, walked through by a fake phone: the steps in order,
// what the projector is told at each, and the profile that comes out.
import { describe, it, expect } from "vitest";
import { CalibrateSheet, SWEEP_HOLD_MS, type Reading } from "../src/calibrate";
import type { Scene, Telemetry } from "../src/calibrate-link";
import { TARGETS, type Profile } from "../src/calibration";
import { LEFT } from "../src/tilt-pad";
import type { PostResult } from "../src/net";
import type { Status } from "../src/types";
import { makePage } from "./harness/page";
import { asDocument, type FakeEl } from "./harness/dom";

const tick = () => new Promise((r) => setTimeout(r, 0));

function setup(opts: { startOk?: boolean; tilt?: boolean } = {}) {
  const doc = makePage();
  let now = 0;
  const scenes: Scene[] = [];
  const tels: Telemetry[] = [];
  const calls: string[] = [];
  const saved: { id: string; body: unknown }[] = [];
  const applied: Profile[] = [];
  let listener: ((r: Reading) => void) | null = null;
  const sheet = new CalibrateSheet(asDocument(doc), {
    api: {
      start: () => {
        calls.push("start");
        return Promise.resolve({
          ok: opts.startOk ?? true,
          data: opts.startOk === false ? { error: "A film is already playing." } : { state: "preparing" },
        } as PostResult);
      },
      profiles: () => Promise.resolve([{ id: "phone-other123", name: "Old phone" }]),
      save: (id, body) => {
        saved.push({ id, body });
        return Promise.resolve({ ok: true, data: { ...(body as object), id, updated_at: 1 } } as PostResult);
      },
    },
    link: {
      open: () => calls.push("open"),
      close: () => calls.push("close"),
      done: () => calls.push("done"),
      setScene: (s) => scenes.push(s),
      setTel: (t) => tels.push(t),
    },
    phoneId: () => "phone-mine1234",
    adoptId: (id) => calls.push("adopt:" + id),
    current: () => null,
    onSaved: (p) => applied.push(p),
    tiltAvailable: opts.tilt ?? true,
    listenTilt: (fn) => {
      listener = fn;
      calls.push("listen");
      return () => {
        listener = null;
        calls.push("unlisten");
      };
    },
    angle: () => 90,
    now: () => now,
    close: () => calls.push("closed"),
  });
  const read = (r: Reading, ms = 50): void => {
    now += ms;
    listener?.(r);
  };
  const el = (id: string) => (sheet.root as unknown as FakeEl).find(id)!;
  const status = (state: string) => sheet.onStatus({ state } as Status);
  return { sheet, scenes, tels, calls, saved, applied, read, el, status, advance: (ms: number) => (now += ms) };
}

type S = ReturnType<typeof setup>;

/** Hold still at (alpha, beta) for one edge. */
function holdEdge(s: S, alpha: number, beta: number): void {
  s.sheet.holdStart();
  for (let t = 0; t <= SWEEP_HOLD_MS + 50; t += 50) {
    s.read({ alpha, beta, gamma: 0 });
  }
}

/** Left at 30, right at 350, top 12 up, bottom 12 down: 40 degrees across,
 * 24 down, the middle at alpha 10. */
function sweep(s: S): void {
  holdEdge(s, 30, 0);
  holdEdge(s, 350, 0);
  holdEdge(s, 10, 12);
  holdEdge(s, 10, -12);
}

/** Point at (x, y) on the picture, as that sweep measured it, for long enough. */
function pointAt(s: S, x: number, y: number): void {
  const alpha = (10 - (x - 0.5) * 40 + 360) % 360;
  const beta = (0.5 - y) * 24;
  for (let i = 0; i < 40; i++) {
    s.read({ alpha, beta, gamma: 0 });
  }
}

async function started(): Promise<S> {
  const s = setup();
  s.sheet.opened();
  s.sheet.name = "Kid's iPhone";
  await s.sheet.begin();
  s.status("calibrating");
  return s;
}

describe("CalibrateSheet", () => {
  it("starts the projector and waits for it", async () => {
    const s = setup();
    s.sheet.opened();
    await s.sheet.begin();
    expect(s.calls).toEqual(expect.arrayContaining(["listen", "open", "start"]));
    expect(s.sheet.step).toBe("starting");
    s.sheet.onStatus({ state: "preparing", prepare: { label: "Waiting for the lamp…" } } as Status);
    expect(s.sheet.step).toBe("starting");
    s.status("calibrating");
    expect(s.sheet.step).toBe("sweep");
    expect(s.scenes.at(-1)).toMatchObject({ step: "sweep", edge: "left" });
  });

  it("says why when the projector is busy", async () => {
    const s = setup({ startOk: false });
    await s.sheet.begin();
    expect(s.sheet.step).toBe("intro");
    expect(s.el("calNote").textContent).toBe("A film is already playing.");
    expect(s.calls).toContain("close");
  });

  it("will not start without the tilt", async () => {
    const s = setup({ tilt: false });
    await s.sheet.begin();
    expect(s.calls).not.toContain("start");
  });

  it("offers this phone the stick's saved profiles", async () => {
    const s = setup();
    s.sheet.opened();
    await tick();
    const pick = (s.el("calBody").children as FakeEl[]).find((c) => c.textContent === "This is Old phone")!;
    pick.click();
    expect(s.calls).toContain("adopt:phone-other123");
    expect(s.sheet.name).toBe("Old phone");
  });

  it("measures the pointer from the four edges", async () => {
    const s = await started();
    sweep(s);
    expect(s.sheet.step).toBe("targets");
    expect(s.sheet.fit!.cal.yaw_across).toBe(40);
    expect(s.sheet.fit!.cal.pitch_across).toBe(24);
  });

  it("does not count a hold let go early", async () => {
    const s = await started();
    s.sheet.holdStart();
    s.read({ alpha: 30, beta: 0, gamma: 0 }, SWEEP_HOLD_MS / 2);
    s.sheet.holdEnd();
    s.read({ alpha: 30, beta: 0, gamma: 0 }, SWEEP_HOLD_MS);
    expect(s.scenes.at(-1)).toMatchObject({ step: "sweep", edge: "left", hold: 0 });
  });

  it("starts the sweep over when it makes no sense", async () => {
    const s = await started();
    holdEdge(s, 10, 0);
    holdEdge(s, 11, 0);
    holdEdge(s, 10, 12);
    holdEdge(s, 10, -12);
    expect(s.sheet.step).toBe("sweep");
    expect(s.el("calNote").textContent).toContain("hardly turned");
  });

  it("hits each target by holding the dot on it, and sends the dot", async () => {
    const s = await started();
    sweep(s);
    for (const t of TARGETS) {
      pointAt(s, t.x, t.y);
    }
    expect(s.sheet.meanMiss).toBeLessThan(0.01);
    expect(s.tels.at(-1)!.dot).not.toBeNull();
    expect(s.scenes.at(-1)!.targets!.every((t) => t.state === "hit")).toBe(true);
  });

  it("measures the gamepad tilt each way, and lights the d-pad", async () => {
    const s = await started();
    sweep(s);
    s.sheet.toTilt();
    s.sheet.tiltReady();
    const rest = { alpha: 0, beta: 0, gamma: -50 };
    s.read(rest); // baseline
    s.read({ ...rest, beta: -20 });
    expect(s.tels.at(-1)!.mask & LEFT).toBe(LEFT);
    s.read(rest);
    s.read({ ...rest, beta: 25 });
    s.read(rest);
    s.read({ ...rest, gamma: -30 });
    s.read(rest);
    s.read({ ...rest, gamma: -65 });
    s.read(rest);
    expect(s.sheet.step).toBe("buttons");
    expect(s.sheet.tilt.press).toEqual({ left: 12, right: 15, up: 12, down: 9 });
    expect(s.sheet.tilt.roll_sign).toBe(1);
  });

  it("swaps an axis that was tilted the other way first", async () => {
    const s = await started();
    sweep(s);
    s.sheet.toTilt();
    s.sheet.tiltReady();
    const rest = { alpha: 0, beta: 0, gamma: -50 };
    s.read(rest);
    s.read({ ...rest, beta: 20 }); // asked for left, went right
    s.read(rest);
    expect(s.sheet.tilt.roll_sign).toBe(-1);
    expect(s.el("calNote").textContent).toContain("Swapped left and right");
  });

  it("gives the buttons their jobs by tapping, and lights them", async () => {
    const s = await started();
    sweep(s);
    s.sheet.toButtons();
    s.sheet.press("B");
    expect(s.tels.at(-1)!.mask).toBe(16); // A's bit, on the left button
    s.sheet.release();
    s.sheet.press("A");
    s.sheet.release();
    s.sheet.press("B");
    expect(s.el("calNote").textContent).toBe("That one is already A.");
    s.sheet.release();
    s.sheet.press("START");
    s.sheet.release();
    s.sheet.press("SELECT");
    s.sheet.release();
    expect(s.sheet.buttons).toEqual({ A: 32, B: 16, START: 64, SELECT: 128 });
  });

  it("saves the profile under this phone's id and applies it", async () => {
    const s = await started();
    sweep(s);
    s.sheet.toTilt();
    s.sheet.toButtons();
    s.sheet.toSummary();
    expect(s.scenes.at(-1)).toMatchObject({ step: "summary", summary: { yaw_across: 40 } });
    await s.sheet.save();
    expect(s.saved[0]!.id).toBe("phone-mine1234");
    expect((s.saved[0]!.body as Profile).name).toBe("Kid's iPhone");
    expect(s.applied[0]!.pointer.yaw_across).toBe(40);
    s.sheet.finish();
    expect(s.calls).toEqual(expect.arrayContaining(["done", "close", "unlisten", "closed"]));
    expect(s.sheet.step).toBe("intro");
  });

  it("gives up when somebody else stops the projector", async () => {
    const s = await started();
    s.status("idle");
    expect(s.sheet.step).toBe("intro");
    expect(s.calls).toContain("done");
  });

  it("takes the black screen away when the sheet is closed", async () => {
    const s = await started();
    s.sheet.closed();
    expect(s.calls).toContain("done");
    expect(s.sheet.step).toBe("intro");
  });
});
