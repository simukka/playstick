// Drawing on the film: that every point of ink gets to the daemon, in order,
// at most one request per tick; that a pen-up cursor is newest-only; and that
// the pad and the tilt move the cursor the way they move the pointer's dot.
import { describe, it, expect } from "vitest";
import { DrawSender, DrawSheet, SYNC_MS, TICK_MS, paintersLine, type DrawReply } from "../src/draw";
import { padMove } from "../src/pointer";
import { DRAW_DEFAULTS, POINTER_DEFAULTS, loadDrawPrefs, saveDrawPrefs } from "../src/prefs";
import { PlayingView } from "../src/views";
import { makePage } from "./harness/page";
import { asDocument, type FakeEl } from "./harness/dom";
import type { PostResult } from "../src/net";
import type { Status } from "../src/types";

const INK = { tool: "brush", color: "red", size: "m" } as const;

function rig(cursor: () => null | { x: number; y: number } = () => null) {
  let t = 0;
  const posts: Record<string, unknown>[] = [];
  const answers: Array<() => void> = [];
  const timers: Array<{ at: number; fn: () => void }> = [];
  const replies: DrawReply[] = [];
  const sender = new DrawSender({
    post: (body) => {
      posts.push(body);
      return new Promise<PostResult>((resolve) =>
        answers.push(() =>
          resolve({ ok: true, data: { shown: true, painters: 2, full: false } as never }),
        ),
      );
    },
    now: () => t,
    schedule: (fn, ms) => timers.push({ at: t + ms, fn }),
    phone: () => "phone-0001",
    cursor: () => {
      const c = cursor();
      return c ? { ...INK, ...c } : null;
    },
    onReply: (r) => replies.push(r),
  });
  const advance = (ms: number): void => {
    t += ms;
    for (const timer of timers.splice(0).filter((x) => x.at <= t)) {
      timer.fn();
    }
  };
  const answer = async (): Promise<void> => {
    answers.shift()?.();
    await Promise.resolve();
    await Promise.resolve();
  };
  return { sender, posts, answer, advance, replies };
}

describe("DrawSender", () => {
  it("drops no ink: points that pile up go in the next request", async () => {
    const r = rig();
    r.sender.points(1, INK, [0.1, 0.1], false);
    r.sender.points(1, INK, [0.2, 0.2], false);
    r.sender.points(1, INK, [0.3, 0.3], true);
    expect(r.posts).toHaveLength(1);
    await r.answer();
    r.advance(TICK_MS);
    expect(r.posts).toHaveLength(2);
    expect(r.posts[1]).toMatchObject({ op: "stroke", id: 1, points: [0.2, 0.2, 0.3, 0.3], end: true });
  });

  it("sends only the newest cursor with the pen up", async () => {
    const r = rig();
    r.sender.start();
    r.sender.cursor({ ...INK, x: 0.1, y: 0.1 });
    r.sender.cursor({ ...INK, x: 0.2, y: 0.2 });
    r.sender.cursor({ ...INK, x: 0.3, y: 0.3 });
    await r.answer();
    r.advance(TICK_MS);
    expect(r.posts).toHaveLength(2);
    expect(r.posts[1]).toMatchObject({ op: "move", x: 0.3, phone: "phone-0001" });
  });

  it("points are not held back behind a wait to check in", async () => {
    const r = rig();
    r.sender.start();
    await r.answer();
    r.sender.points(1, INK, [0.5, 0.5], false);
    r.advance(TICK_MS);
    expect(r.posts[1]).toMatchObject({ op: "stroke" });
  });

  it("checks in every SYNC_MS, keeping a held-still cursor alive", async () => {
    let held: { x: number; y: number } | null = null;
    const r = rig(() => held);
    r.sender.start();
    expect(r.posts[0]).toMatchObject({ op: "sync" });
    await r.answer();
    expect(r.replies[0]!.painters).toBe(2);
    held = { x: 0.4, y: 0.6 };
    r.advance(SYNC_MS);
    expect(r.posts[1]).toMatchObject({ op: "move", x: 0.4, y: 0.6 });
  });

  it("leaves after its last points, then goes quiet", async () => {
    const r = rig();
    r.sender.start();
    r.sender.points(1, INK, [0.1, 0.1], true);
    r.sender.stop();
    for (let i = 0; i < 4; i++) {
      await r.answer();
      r.advance(TICK_MS);
    }
    expect(r.posts.map((p) => p.op)).toEqual(["sync", "stroke", "leave"]);
    r.advance(SYNC_MS * 4);
    expect(r.posts).toHaveLength(3);
  });
});

describe("paintersLine", () => {
  it("counts the others", () => {
    expect(paintersLine(1)).toBe("Only you are drawing.");
    expect(paintersLine(2)).toBe("You and 1 other are drawing.");
    expect(paintersLine(4)).toBe("You and 3 others are drawing.");
  });
});

describe("draw prefs", () => {
  it("each tool keeps its own size, and junk falls back to the defaults", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    saveDrawPrefs(storage, { tool: "pencil", color: "blue", sizes: { brush: "l", pencil: "s", eraser: "m" } });
    expect(loadDrawPrefs(storage)).toEqual({ tool: "pencil", color: "blue", sizes: { brush: "l", pencil: "s", eraser: "m" } });
    store.set("playstick.draw", '{"tool":"spray","color":"&H00&","sizes":{"brush":"xl"}}');
    expect(loadDrawPrefs(storage)).toEqual(DRAW_DEFAULTS);
    const broken = { getItem: () => { throw new Error("no"); }, setItem: () => { throw new Error("no"); } };
    expect(loadDrawPrefs(broken)).toEqual(DRAW_DEFAULTS);
  });
});

describe("DrawSheet", () => {
  const playing = { state: "playing" } as Status;

  function sheet(tilt = false) {
    const sent: unknown[][] = [];
    const sender = {
      start() {},
      stop() {},
      clear: () => sent.push(["clear"]),
      points: (...a: unknown[]) => sent.push(["points", ...a]),
      cursor: (c: unknown) => sent.push(["cursor", c]),
    } as unknown as DrawSender;
    let listening = 0;
    const s = new DrawSheet(asDocument(makePage()), {
      sender,
      storage: null,
      tiltAvailable: tilt,
      listenTilt: () => {
        listening++;
        return () => listening--;
      },
    });
    s.onStatus(playing);
    s.paint();
    const kids = s.root.children as unknown as FakeEl[];
    return { s, sent, pad: kids[1]!, aimBtn: kids[2]!, listening: () => listening };
  }

  it("with the pen down a drag is one line, moved like the pointer's dot", () => {
    const { s, sent, pad } = sheet();
    pad.fire("pointerdown", { clientX: 100, clientY: 100 });
    pad.fire("pointermove", { clientX: 160, clientY: 100 });
    pad.fire("pointerup");
    const want = padMove({ x: 0.5, y: 0.5 }, 60, 0, 300, POINTER_DEFAULTS.speed);
    expect(s.dot).toEqual(want);
    expect(sent.map((c) => c[0])).toEqual(["points", "points", "points"]);
    expect(sent[0]![1]).toBe(1);
    expect(sent[0]![3]).toEqual([0.5, 0.5]);
    expect(sent[1]![3]).toEqual([want.x, want.y]);
    expect(sent[2]![4]).toBe(true);
  });

  it("with the pen up a drag only moves the cursor", () => {
    const { s, sent, pad } = sheet();
    s.pen = false;
    pad.fire("pointerdown", { clientX: 100, clientY: 100 });
    pad.fire("pointermove", { clientX: 130, clientY: 100 });
    pad.fire("pointerup");
    expect(sent.map((c) => c[0])).toEqual(["cursor", "cursor"]);
  });

  it("the next line starts where the cursor was left", () => {
    const { s, sent, pad } = sheet();
    pad.fire("pointerdown", { clientX: 0, clientY: 0 });
    pad.fire("pointermove", { clientX: 30, clientY: 0 });
    pad.fire("pointerup");
    const left = s.dot;
    pad.fire("pointerdown", { clientX: 200, clientY: 200 });
    expect(sent[sent.length - 1]![3]).toEqual([left.x, left.y]);
    expect(sent[sent.length - 1]![1]).toBe(2);
  });

  it("draws nothing with no film up", () => {
    const { s, sent, pad } = sheet();
    s.onStatus({ state: "idle" } as Status);
    pad.fire("pointerdown");
    expect(sent).toEqual([]);
  });

  it("holding the aim button and tilting draws, and letting go ends the line", () => {
    const { s, sent, aimBtn, listening } = sheet(true);
    aimBtn.fire("pointerdown");
    expect(listening()).toBe(1);
    s.onTilt({ alpha: 10, beta: 0 });
    s.onTilt({ alpha: 0, beta: 0 });
    expect(s.dot.x).toBeGreaterThan(0.5);
    aimBtn.fire("pointerup");
    expect(listening()).toBe(0);
    expect(sent[sent.length - 1]![4]).toBe(true);
  });

  it("closing mid-line ends the line", () => {
    const { s, sent, pad } = sheet();
    pad.fire("pointerdown");
    s.closed();
    expect(sent[sent.length - 1]![4]).toBe(true);
  });
});

describe("the Draw button", () => {
  it("sits right under STOP", () => {
    const v = new PlayingView(asDocument(makePage()));
    const kids = v.root.children as unknown as FakeEl[];
    const stop = kids.indexOf(v.stop as unknown as FakeEl);
    expect(kids[stop + 1]).toBe(v.draw as unknown as FakeEl);
  });
});
