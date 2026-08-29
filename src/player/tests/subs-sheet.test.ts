// The subtitle sheet: what it offers, and what it says when there is nothing to
// offer. The empty case is the one worth a test -- a film whose only subtitles
// are the image-based kind prep declines to OCR looks identical from here to a
// library nobody has prepped, and a sheet that just showed "Off" would leave
// somebody tapping it wondering which of those had happened.
import { describe, it, expect } from "vitest";
import { SubsSheet } from "../src/subs-sheet";
import { makePage } from "./harness/page";
import { asDocument, type FakeEl } from "./harness/dom";
import type { Status } from "../src/types";

const TRACKS = [
  { n: 1, lang: "eng" },
  { n: 2, lang: "fin", title: "Suomi (forced)" },
];

function harness(status: Partial<Status> = {}) {
  const doc = makePage();
  const chosen: number[] = [];
  const sheet = new SubsSheet(asDocument(doc), { choose: (t) => chosen.push(t) });
  sheet.onStatus({ state: "playing", subs: TRACKS, sub: 0, ...status } as Status);
  sheet.paint();
  const rows = () => (doc.getElementById("subsList") as FakeEl).children;
  // row() wraps the label in a span, so the button's own textContent is empty.
  const labels = () => rows().map((r) => r.children[0]!.textContent);
  return { doc, sheet, chosen, rows, labels };
}

describe("SubsSheet", () => {
  it("offers Off first, then one row per track", () => {
    const h = harness();
    expect(h.labels()).toEqual(["Off", "English", "Suomi (forced)"]);
  });

  it("ticks the track that is on", () => {
    const h = harness({ sub: 2 });
    const sel = h.rows().filter((r) => r.classList.contains("sel"));
    expect(sel.map((r) => r.children[0]!.textContent)).toEqual(["Suomi (forced)"]);
  });

  it("ticks Off when nothing is on", () => {
    const h = harness({ sub: 0 });
    expect(h.rows()[0]!.classList.contains("sel")).toBe(true);
  });

  it("a tap chooses that track", () => {
    const h = harness();
    h.rows()[1]!.click();
    h.rows()[0]!.click();
    expect(h.chosen).toEqual([1, 0]);
  });

  it("says what to do about a film with no subtitles", () => {
    const h = harness({ subs: [] });
    // Off is still offered rather than an empty list, so the sheet always has
    // the one row that undoes whatever the last person did.
    expect(h.labels()).toEqual(["Off"]);
    expect(h.doc.getElementById("subsNote")!.textContent).toContain(
      "playstick-prep.py",
    );
  });

  it("says so when the film ended under an open sheet", () => {
    const h = harness();
    h.sheet.onStatus({ state: "idle" } as Status);
    h.sheet.paint();
    expect(h.rows()).toHaveLength(0);
    expect(h.doc.getElementById("subsNote")!.textContent).toContain("Start a film");
  });

  it("repaints rather than accumulating rows", () => {
    const h = harness();
    h.sheet.paint();
    h.sheet.paint();
    expect(h.rows()).toHaveLength(3);
  });
});
