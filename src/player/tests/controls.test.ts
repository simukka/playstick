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
  let toLibrary = 0;
  let clock = 1000;
  new Controls(
    {
      toggle: playing.toggle,
      stop: playing.stop,
      cancel: preparing.cancel,
      volDown: playing.volDown,
      volUp: playing.volUp,
    },
    api,
    {
      stateOf: () => state,
      apply: (s) => applied.push(s),
      toLibrary: () => toLibrary++,
      now: () => clock,
    },
  );
  return {
    doc,
    calls,
    applied,
    setClock: (t: number) => (clock = t),
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
});
