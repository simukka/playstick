// The pointer: where a drag and a tilt put the dot, how often it is sent, and
// that a setting survives storage that refuses to work.
import { describe, it, expect } from "vitest";
import {
  PointerSender,
  PointerSheet,
  TICK_MS,
  aim,
  angleDelta,
  padMove,
  type Orientation,
  type Sample,
} from "../src/pointer";
import { POINTER_DEFAULTS, loadPointerPrefs, savePointerPrefs } from "../src/prefs";
import { makePage } from "./harness/page";
import { asDocument, type FakeEl } from "./harness/dom";
import type { PostResult } from "../src/net";
import type { Status } from "../src/types";

describe("padMove", () => {
  it("moves a pad's width times the speed across the screen", () => {
    expect(padMove({ x: 0.2, y: 0.5 }, 100, 0, 200, 1).x).toBeCloseTo(0.7);
  });

  it("moves the same distance up as across, on a 16:9 screen", () => {
    const p = padMove({ x: 0.5, y: 0.5 }, 0, 9, 160, 1);
    expect(p.y).toBeCloseTo(0.5 + (9 / 160) * (16 / 9));
  });

  it("stops at the edges", () => {
    expect(padMove({ x: 0.9, y: 0.1 }, 500, -500, 100, 2)).toEqual({ x: 1, y: 0 });
  });
});

describe("aim", () => {
  const base: Orientation = { alpha: 10, beta: 20 };

  it("turning right moves the dot right", () => {
    expect(aim({ x: 0.5, y: 0.5 }, base, { alpha: 5, beta: 20 }, 1).x).toBeGreaterThan(0.5);
  });

  it("tilting up moves the dot up", () => {
    expect(aim({ x: 0.5, y: 0.5 }, base, { alpha: 10, beta: 25 }, 1).y).toBeLessThan(0.5);
  });

  it("turning through north is a small turn, not a full circle", () => {
    expect(angleDelta(5, 355)).toBe(10);
    expect(angleDelta(355, 5)).toBe(-10);
    const p = aim({ x: 0.5, y: 0.5 }, { alpha: 2, beta: 0 }, { alpha: 358, beta: 0 }, 1);
    expect(p.x).toBeGreaterThan(0.5);
    expect(p.x).toBeLessThan(0.7);
  });
});

function sender() {
  let now = 0;
  const posted: Sample[] = [];
  const later: (() => void)[] = [];
  let resolve: ((r: PostResult) => void) | null = null;
  const s = new PointerSender({
    post: (sample) => {
      posted.push(sample);
      return new Promise((r) => (resolve = r));
    },
    now: () => now,
    schedule: (fn) => later.push(fn),
  });
  return {
    s,
    posted,
    advance: (ms: number) => (now += ms),
    answer: async () => {
      resolve?.({ ok: true, data: { shown: true } });
      await new Promise((r) => setTimeout(r, 0));
    },
    runLater: () => later.splice(0).forEach((fn) => fn()),
  };
}

const at = (x: number): Sample => ({ x, y: 0.5, on: true, size: "m", color: "red" });

describe("PointerSender", () => {
  it("sends the first sample at once", () => {
    const h = sender();
    h.s.send(at(0.1));
    expect(h.posted).toEqual([at(0.1)]);
  });

  it("keeps one request in flight and sends only the newest after it", async () => {
    const h = sender();
    h.s.send(at(0.1));
    for (let i = 2; i <= 9; i++) {
      h.s.send(at(i / 10));
    }
    expect(h.posted).toHaveLength(1);
    h.advance(TICK_MS);
    await h.answer();
    expect(h.posted.map((p) => p.x)).toEqual([0.1, 0.9]);
  });

  it("waits out the tick when the answer comes back early", async () => {
    const h = sender();
    h.s.send(at(0.1));
    h.s.send(at(0.2));
    await h.answer();
    expect(h.posted).toHaveLength(1);
    h.advance(TICK_MS);
    h.runLater();
    expect(h.posted.map((p) => p.x)).toEqual([0.1, 0.2]);
  });
});

describe("prefs", () => {
  it("falls back to the defaults when storage throws", () => {
    const broken = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceeded");
      },
    };
    expect(loadPointerPrefs(broken)).toEqual(POINTER_DEFAULTS);
    expect(() => savePointerPrefs(broken, POINTER_DEFAULTS)).not.toThrow();
  });

  it("round-trips, and ignores values it does not know", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    };
    savePointerPrefs(storage, { speed: 2, size: "l", color: "green", tilt: false });
    expect(loadPointerPrefs(storage)).toEqual({ speed: 2, size: "l", color: "green", tilt: false });
    store.set("playstick.pointer", JSON.stringify({ speed: 99, size: "xl", color: "&H00" }));
    expect(loadPointerPrefs(storage)).toEqual({ ...POINTER_DEFAULTS, speed: 3 });
  });
});

function sheet(opts: { tilt?: boolean; ask?: boolean; state?: string } = {}) {
  const doc = makePage();
  const sent: Sample[] = [];
  const fakeSender = { send: (s: Sample) => sent.push(s) } as unknown as PointerSender;
  let listener: ((o: Orientation) => void) | null = null;
  const later: (() => void)[] = [];
  const p = new PointerSheet(asDocument(doc), {
    sender: fakeSender,
    storage: null,
    schedule: (fn) => later.push(fn),
    cancel: () => {},
    tiltAvailable: !!opts.tilt,
    requestTilt: opts.ask ? () => Promise.resolve(true) : undefined,
    listenTilt: (fn) => {
      listener = fn;
      return () => (listener = null);
    },
  });
  p.onStatus({ state: opts.state ?? "playing" } as Status);
  p.paint();
  const el = (id: string) => doc.getElementById(id) as FakeEl;
  return { p, sent, el, tilt: (o: Orientation) => listener?.(o), later };
}

describe("PointerSheet", () => {
  it("a drag moves the dot and shows it; lifting hides it a moment later", () => {
    const h = sheet();
    const pad = h.el("pointerPad");
    pad.fire("pointerdown", { clientX: 100, clientY: 100 });
    pad.fire("pointermove", { clientX: 130, clientY: 100 });
    const last = h.sent[h.sent.length - 1]!;
    expect(last.on).toBe(true);
    expect(last.x).toBeCloseTo(0.5 + (30 / 300) * 1.5);
    pad.fire("pointerup");
    h.later.splice(0).forEach((fn) => fn());
    expect(h.sent[h.sent.length - 1]!.on).toBe(false);
  });

  it("does nothing on the pad without a film, and says so", () => {
    const h = sheet({ state: "idle" });
    h.el("pointerPad").fire("pointerdown", { clientX: 1, clientY: 1 });
    expect(h.sent).toEqual([]);
    expect(h.el("pointerPad").textContent).toContain("Start one first");
  });

  it("hides the aim button on plain http", () => {
    expect(sheet({ tilt: false }).el("aimBtn").classList.contains("on")).toBe(false);
  });

  it("asks first where the browser wants asking", async () => {
    const h = sheet({ tilt: true, ask: true });
    expect(h.el("aimBtn").textContent).toBe("Allow tilting");
    h.el("aimBtn").click();
    await new Promise((r) => setTimeout(r, 0));
    expect(h.el("aimBtn").textContent).toBe("Hold to aim");
  });

  it("aims from where the dot is, only while held", () => {
    const h = sheet({ tilt: true });
    const btn = h.el("aimBtn");
    btn.fire("pointerdown");
    h.tilt({ alpha: 50, beta: 10 });           // the reading it starts from
    h.tilt({ alpha: 40, beta: 10 });           // ten degrees to the right
    expect(h.p.dot.x).toBeGreaterThan(0.5);
    btn.fire("pointerup");
    const x = h.p.dot.x;
    h.tilt({ alpha: 0, beta: 10 });
    expect(h.p.dot.x).toBe(x);
  });

  it("closing hides the dot", () => {
    const h = sheet();
    h.p.closed();
    expect(h.sent[h.sent.length - 1]!.on).toBe(false);
  });
});
