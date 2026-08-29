import { describe, it, expect } from "vitest";
import { Controls } from "../src/controls";
import { ApiClient } from "../src/net";
import { PlayingView, PreparingView } from "../src/views";
import { makePage } from "./harness/page";
import { asDocument } from "./harness/dom";
import type { PlayerState, Status } from "../src/types";

const flush = () => new Promise((r) => setTimeout(r, 0));

function harness(state: PlayerState = "playing") {
  const doc = makePage();
  // The real buttons, in the real views they are drawn in.
  const playing = new PlayingView(asDocument(doc));
  const preparing = new PreparingView(asDocument(doc), (p) => p);
  const calls: Array<{ url: string; body: unknown }> = [];
  const api = new ApiClient((url, init) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ state: "paused" }) });
  });
  const applied: Status[] = [];
  const previews: number[] = [];
  let toLibrary = 0;
  let clock = 1000;
  // A hand-driven timer queue: the pad schedules a repaint tick while held and
  // a flush after the last press, and a test has to be able to stand between
  // the two rather than wait 350 ms for every assertion.
  const timers = new Map<number, { fn: () => void; at: number }>();
  let nextTimer = 1;
  const controls = new Controls(
    {
      toggle: playing.toggle,
      stop: playing.stop,
      cancel: preparing.cancel,
      volDown: playing.volDown,
      volUp: playing.volUp,
      skipBack: playing.skipBack,
      skipFwd: playing.skipFwd,
    },
    api,
    {
      stateOf: () => state,
      apply: (s) => applied.push(s),
      toLibrary: () => toLibrary++,
      onSeekPreview: (p) => previews.push(p),
      now: () => clock,
      schedule: (fn, ms) => {
        const id = nextTimer++;
        timers.set(id, { fn, at: clock + ms });
        return id;
      },
      cancelTimer: (id) => void timers.delete(id),
    },
  );
  /** Run every timer due at or before the current clock, repeatedly, so a tick
   * that reschedules itself is followed rather than run once. */
  const run = () => {
    for (let guard = 0; guard < 500; guard++) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= clock);
      if (!due.length) {
        return;
      }
      for (const [id, t] of due) {
        timers.delete(id);
        t.fn();
      }
    }
  };
  return {
    doc,
    calls,
    applied,
    previews,
    controls,
    setClock: (t: number) => {
      clock = t;
      run();
    },
    get toLibrary() {
      return toLibrary;
    },
  };
}

describe("Controls", () => {
  it("pauses a playing film and applies the result", async () => {
    const h = harness("playing");
    h.doc.getElementById("toggle")!.click();
    await flush();
    expect(h.calls[0]!.url).toBe("/api/pause");
    expect(h.applied).toHaveLength(1);
  });

  it("resumes a paused film", async () => {
    const h = harness("paused");
    h.doc.getElementById("toggle")!.click();
    await flush();
    expect(h.calls[0]!.url).toBe("/api/resume");
  });

  it("debounces a double tap", async () => {
    const h = harness("playing");
    h.doc.getElementById("toggle")!.click();
    h.doc.getElementById("toggle")!.click(); // same instant
    await flush();
    expect(h.calls).toHaveLength(1);
    // Past the 800 ms lock, it fires again.
    h.setClock(2000);
    h.doc.getElementById("toggle")!.click();
    await flush();
    expect(h.calls).toHaveLength(2);
  });

  it("stop and cancel drop to the library and hit /api/stop", async () => {
    const h = harness("playing");
    h.doc.getElementById("stop")!.click();
    await flush();
    expect(h.toLibrary).toBe(1);
    expect(h.calls[0]!.url).toBe("/api/stop");

    h.setClock(3000);
    h.doc.getElementById("prepCancel")!.click();
    await flush();
    expect(h.toLibrary).toBe(2);
    expect(h.calls[1]!.url).toBe("/api/stop");
  });

  it("nudges the volume without a guard", async () => {
    const h = harness("playing");
    h.doc.getElementById("volDown")!.click();
    h.doc.getElementById("volUp")!.click();
    await flush();
    expect(h.calls.map((c) => [c.url, c.body])).toEqual([
      ["/api/volume", { delta: -10 }],
      ["/api/volume", { delta: 10 }],
    ]);
  });

  const tap = (h: ReturnType<typeof harness>, id: string) => {
    h.doc.getElementById(id)!.fire("pointerdown");
    h.doc.getElementById(id)!.fire("pointerup");
  };

  it("sends one seek for one tap, after the flush", async () => {
    const h = harness("playing");
    tap(h, "skipFwd");
    expect(h.calls).toHaveLength(0); // nothing yet: the pad is still collecting
    h.setClock(1400);
    await flush();
    expect(h.calls.map((c) => [c.url, c.body])).toEqual([
      ["/api/seek", { delta: 10 }],
    ]);
    expect(h.applied).toHaveLength(1);
  });

  it("three quick taps are one request for thirty seconds", async () => {
    const h = harness("playing");
    tap(h, "skipFwd");
    h.setClock(1120);
    tap(h, "skipFwd");
    h.setClock(1240);
    tap(h, "skipFwd");
    h.setClock(1700);
    await flush();
    expect(h.calls.map((c) => c.body)).toEqual([{ delta: 30 }]);
  });

  it("a tap the other way takes it back, and sends nothing", async () => {
    const h = harness("playing");
    tap(h, "skipFwd");
    h.setClock(1100);
    tap(h, "skipBack");
    h.setClock(1600);
    await flush();
    expect(h.calls).toHaveLength(0);
    expect(h.previews.at(-1)).toBe(0);
  });

  it("a three-second hold is half an hour", async () => {
    const h = harness("playing");
    h.doc.getElementById("skipFwd")!.fire("pointerdown");
    expect(h.previews.at(-1)).toBe(10);
    expect(h.doc.getElementById("skipFwd")!.classList.contains("held")).toBe(true);
    h.setClock(4000); // three seconds down
    h.doc.getElementById("skipFwd")!.fire("pointerup");
    expect(h.previews.at(-1)).toBe(1800);
    expect(h.doc.getElementById("skipFwd")!.classList.contains("held")).toBe(false);
    h.setClock(4400);
    await flush();
    expect(h.calls.map((c) => c.body)).toEqual([{ delta: 1800 }]);
  });

  it("a cancelled hold sends nothing", async () => {
    const h = harness("playing");
    h.doc.getElementById("skipFwd")!.fire("pointerdown");
    h.setClock(3000);
    h.doc.getElementById("skipFwd")!.fire("pointercancel");
    h.setClock(4000);
    await flush();
    expect(h.calls).toHaveLength(0);
  });

  it("stopping the film drops a queued skip", async () => {
    const h = harness("playing");
    tap(h, "skipFwd");
    h.doc.getElementById("stop")!.click();
    h.setClock(2000);
    await flush();
    expect(h.calls.map((c) => c.url)).toEqual(["/api/stop"]);
  });
});
