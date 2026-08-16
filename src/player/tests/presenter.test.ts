import { describe, it, expect } from "vitest";
import { StatusPresenter } from "../src/presenter";
import { ReloadPolicy } from "../src/build";
import { makeShell } from "./harness/page";
import { asDocument } from "./harness/dom";
import type { Status } from "../src/types";

function present() {
  const shell = makeShell();
  const doc = shell.doc;
  const reloads: number[] = [];
  const reload = new ReloadPolicy({ reload: () => reloads.push(1) }, "here");
  let clock = 1000;
  const p = new StatusPresenter(asDocument(doc), {
    reload,
    stamp: (path) => path + "?v=here",
    now: () => clock,
    ...shell.parts,
  });
  return { doc, p, reloads, setClock: (t: number) => (clock = t) };
}

function status(over: Partial<Status>): Status {
  return { state: "idle", ...over } as Status;
}

const on = (doc: ReturnType<typeof makeShell>["doc"], id: string) =>
  doc.getElementById(id)!.classList.contains("on");
const text = (doc: ReturnType<typeof makeShell>["doc"], id: string) =>
  doc.getElementById(id)!.textContent;

describe("StatusPresenter view switching", () => {
  it("starts on the library", () => {
    const { doc, p } = present();
    p.apply(status({ state: "idle" }));
    expect(on(doc, "library")).toBe(true);
    expect(on(doc, "playing")).toBe(false);
    expect(on(doc, "filterBtn")).toBe(true);
  });

  it("shows the preparing view with the server's step and poster", () => {
    const { doc, p } = present();
    p.apply(
      status({ state: "preparing", id: "abc", title: "Ponyo", prepare: { label: "Waiting for the lamp\u2026" } }),
    );
    expect(on(doc, "preparing")).toBe(true);
    expect(on(doc, "library")).toBe(false);
    expect(text(doc, "prepTitle")).toBe("Ponyo");
    expect(text(doc, "prepStep")).toBe("Waiting for the lamp\u2026");
    expect((doc.getElementById("prepArt") as { src: string }).src).toBe("/api/thumb/abc?v=here");
  });

  it("shows the playing view with countdown, toggle and volume", () => {
    const { doc, p } = present();
    p.apply(
      status({ state: "playing", title: "Dune", duration: 7200, position: 3600, audio: true }),
    );
    expect(on(doc, "playing")).toBe(true);
    expect(text(doc, "nowTitle")).toBe("Dune");
    expect(text(doc, "nowSub")).toBe("1 h left");
    expect(on(doc, "volume")).toBe(true);
    expect((doc.getElementById("barFill") as { style: Record<string, string> }).style.width).toBe("50.0%");
  });

  it("says Paused and shows the play glyph when paused", () => {
    const { doc, p } = present();
    p.apply(status({ state: "paused", title: "Dune", duration: 7200, position: 100 }));
    expect(text(doc, "nowSub")).toBe("Paused");
    expect(doc.getElementById("toggle")!.innerHTML).toBe("\u25B6");
  });
});

describe("StatusPresenter banners", () => {
  it("warns about the projector only once the film is up", () => {
    const { doc, p } = present();
    p.apply(status({ state: "playing", projector: { model: "x", power: "on", fault: "no link" } }));
    expect(on(doc, "banner")).toBe(true);
    expect(text(doc, "banner")).toMatch(/couldn't reach the projector/);
  });

  it("explains an AirPlay takeover and blocks the grid", () => {
    const { doc, p } = present();
    p.apply(status({ state: "airplay" }));
    expect(text(doc, "banner")).toMatch(/using the projector/);
    expect(doc.getElementById("grid")!.classList.contains("blocked")).toBe(true);
  });

  it("surfaces a give-up notice from a background prepare", () => {
    const { doc, p } = present();
    p.apply(status({ state: "idle", notice: "gave up preparing Ponyo" }));
    expect(text(doc, "banner")).toBe("gave up preparing Ponyo");
  });

  it("clears the banner on a clean idle", () => {
    const { doc, p } = present();
    p.apply(status({ state: "airplay" }));
    p.apply(status({ state: "idle" }));
    expect(on(doc, "banner")).toBe(false);
  });
});

describe("StatusPresenter reload", () => {
  it("reloads on a build mismatch from a resting state", () => {
    const { p, reloads } = present();
    p.apply(status({ state: "idle", build: "there" }));
    expect(reloads).toHaveLength(1);
  });

  it("defers a reload while a film is up", () => {
    const { p, reloads } = present();
    p.apply(status({ state: "playing", build: "there" }));
    expect(reloads).toHaveLength(0);
  });
});
