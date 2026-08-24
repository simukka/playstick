import { describe, it, expect } from "vitest";
import { row, langName } from "../src/sheet";
import { makePage, makeShell } from "./harness/page";
import { asDocument, FakeEl } from "./harness/dom";

describe("row", () => {
  it("builds a tappable row with a tick when selected", () => {
    const doc = makePage();
    let taps = 0;
    const built = row(asDocument(doc), "English", "5.1", true, () => taps++) as unknown as FakeEl;
    expect(built.children[0]!.children[0]!.textContent).toBe("5.1"); // the sub-label
    expect(built.classList.contains("sel")).toBe(true);
    built.click();
    expect(taps).toBe(1);
  });
});

describe("langName", () => {
  it("prefers an explicit title, then the map, then an upper-cased code", () => {
    expect(langName("eng")).toBe("English");
    expect(langName("fin")).toBe("Suomi");
    expect(langName("xyz")).toBe("XYZ");
    expect(langName("und")).toBe("Sound");
    expect(langName("eng", "Director's commentary")).toBe("Director's commentary");
  });
});

describe("SheetManager", () => {
  function mgr() {
    const shell = makeShell();
    return {
      doc: shell.doc,
      m: shell.sheets,
      get soundPaints() { return shell.soundPaints(); },
      get filterPaints() { return shell.filterPaints(); },
    };
  }

  it("opens the sound sheet from the audio button and paints it", () => {
    const h = mgr();
    h.doc.getElementById("audioBtn")!.click();
    expect(h.m.soundOpen).toBe(true);
    expect(h.doc.getElementById("sheetScrim")!.classList.contains("on")).toBe(true);
    expect(h.soundPaints).toBe(1);
  });

  it("keeps only one sheet open at a time", () => {
    const h = mgr();
    h.m.showSound(true);
    h.m.showFilter(true);
    expect(h.m.soundOpen).toBe(false);
    expect(h.m.filterOpen).toBe(true);
  });

  it("the scrim closes everything", () => {
    const h = mgr();
    h.m.showSound(true);
    h.doc.getElementById("sheetScrim")!.click();
    expect(h.m.soundOpen).toBe(false);
    expect(h.doc.getElementById("sheetScrim")!.classList.contains("on")).toBe(false);
  });
});
