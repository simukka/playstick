// The gamepad view: thumbs and tilt make one mask, the seat the daemon gives
// decides what is offered, and leaving tears the socket and sound down.
import { describe, it, expect } from "vitest";
import { GamepadView } from "../src/gamepad";
import { A, B, LEFT } from "../src/tilt-pad";
import { makePage } from "./harness/page";
import { asDocument, type FakeEl } from "./harness/dom";
import type { GameLink } from "../src/game-link";
import type { GameAudio } from "../src/game-audio";
import type { Status } from "../src/types";

function setup(opts: { tilt?: boolean; ask?: boolean } = {}) {
  const doc = makePage();
  const masks: number[] = [];
  const calls: string[] = [];
  let tiltFn: ((t: { beta: number; gamma: number }) => void) | null = null;
  let audioOn = false;
  const link = {
    open: () => calls.push("open"),
    close: () => calls.push("close"),
    join: (how: string) => calls.push("join:" + how),
    setMask: (m: number) => masks.push(m),
  } as unknown as GameLink;
  const audio = {
    get on() {
      return audioOn;
    },
    unlock: () => calls.push("unlock"),
    start: (rate: number) => {
      audioOn = true;
      calls.push("sound:" + rate);
    },
    stop: () => {
      audioOn = false;
      calls.push("silence");
    },
  } as unknown as GameAudio;
  const view = new GamepadView(asDocument(doc), {
    link,
    audio,
    tiltAvailable: opts.tilt ?? true,
    requestTilt: opts.ask ? () => Promise.resolve(true) : undefined,
    listenTilt: (fn) => {
      tiltFn = fn;
      calls.push("listen");
      return () => {
        tiltFn = null;
        calls.push("unlisten");
      };
    },
    angle: () => 90,
    stop: () => calls.push("stop"),
    schedule: () => 1,
    cancel: () => {},
  });
  const el = (id: string) => (view.root as unknown as FakeEl).find(id)!;
  const tilt = (beta: number) => tiltFn?.({ beta, gamma: -50 });
  return { view, el, masks, calls, tilt };
}

const gaming = (over: Partial<Status> = {}): Status =>
  ({ state: "gaming", title: "Lan Master", game: { rate: 24000, players: 2, seats: [true, false] }, ...over }) as Status;

describe("GamepadView", () => {
  it("opens the socket and listens to the tilt when a game comes up", () => {
    const { view, calls } = setup();
    view.apply(gaming());
    expect(calls).toContain("open");
    expect(calls).toContain("listen");
  });

  it("holds a thumb button while it is touched", () => {
    const { view, el, masks } = setup();
    view.apply(gaming());
    el("padA").fire("pointerdown");
    el("padB").fire("pointerdown");
    expect(masks.at(-1)).toBe(A | B);
    el("padA").fire("pointerup");
    expect(masks.at(-1)).toBe(B);
    el("padB").fire("pointerleave");
    expect(masks.at(-1)).toBe(0);
  });

  it("adds the tilt to the thumbs", () => {
    const { view, el, masks, tilt } = setup();
    view.apply(gaming());
    tilt(0); // baseline
    el("padA").fire("pointerdown");
    tilt(-20);
    expect(masks.at(-1)).toBe(A | LEFT);
  });

  it("sends what a profile says each button sends, and wears its name", () => {
    const { view, el, masks } = setup();
    view.apply(gaming());
    view.setCalibration(null, { A: B, B: A, START: 64, SELECT: 128 });
    expect(el("padA").textContent).toBe("B");
    el("padA").fire("pointerdown");
    expect(masks.at(-1)).toBe(B);
    el("padA").fire("pointerup");
    expect(masks.at(-1)).toBe(0);
    view.setCalibration(null, null);
    expect(el("padA").textContent).toBe("A");
  });

  it("lets go of the bit it pressed, even if the map changed under the thumb", () => {
    const { view, el, masks } = setup();
    view.apply(gaming());
    el("padA").fire("pointerdown");
    view.setCalibration(null, { A: B, B: A, START: 64, SELECT: 128 });
    el("padA").fire("pointerup");
    expect(masks.at(-1)).toBe(0);
  });

  it("offers player two to a listener only when that seat is free", () => {
    const { view, el } = setup();
    view.apply(gaming());
    view.onRole("listener", { players: 2, seats: [true, false] });
    expect(el("padJoin").hidden).toBe(false);
    view.onRole("listener", { players: 2, seats: [true, true] });
    expect(el("padJoin").hidden).toBe(true);
    view.onRole("listener", { players: 1, seats: [true, false] });
    expect(el("padJoin").hidden).toBe(true);
  });

  it("joining is also the tap that switches the sound on", () => {
    const { view, el, calls } = setup();
    view.apply(gaming());
    view.onRole("listener", { players: 2, seats: [true, false], rate: 24000 });
    el("padJoin").click();
    expect(calls).toContain("unlock");
    expect(calls).toContain("sound:24000");
    expect(calls).toContain("join:any");
  });

  it("names the seat it was given", () => {
    const { view, el } = setup();
    view.apply(gaming());
    view.onRole("p1", { players: 2, seats: [true, false] });
    expect(el("padRole").textContent).toBe("Player 1");
  });

  it("tears down the socket, the sound and the tilt when the game ends", () => {
    const { view, calls } = setup();
    view.apply(gaming());
    view.leave();
    expect(calls).toEqual(expect.arrayContaining(["close", "silence", "unlisten"]));
  });

  it("says why there is no tilt on plain http", () => {
    const { view, el } = setup({ tilt: false });
    view.apply(gaming());
    expect(el("padTilt").textContent).toContain("https");
  });
});
