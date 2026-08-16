import { describe, it, expect } from "vitest";
import { FilterSheet } from "../src/filter-sheet";
import { LibraryModel } from "../src/library";
import { makePage } from "./harness/page";
import { asDocument, FakeEl } from "./harness/dom";
import type { LibraryItem } from "../src/types";

const items: LibraryItem[] = [
  { id: "a", title: "Akira", year: 1988, rating: "8.0", genres: ["Anime"], audio_langs: ["jpn"] },
  { id: "b", title: "Brave", year: 2012, rating: "7.1", genres: ["Family"] },
];

function harness(lib: LibraryItem[] = items) {
  const doc = makePage();
  const model = new LibraryModel();
  model.setItems(lib);
  let changes = 0;
  const sheet = new FilterSheet(asDocument(doc), model, () => changes++);
  return { doc, model, sheet, get changes() { return changes; } };
}

const el = (doc: ReturnType<typeof makePage>, id: string) =>
  doc.getElementById(id) as unknown as FakeEl;

describe("FilterSheet", () => {
  it("lists sort orders, genres, scores and readiness for a prepared library", () => {
    const h = harness();
    h.sheet.paint();
    expect(el(h.doc, "sortList").children.length).toBe(3);
    expect(el(h.doc, "genreList").children.length).toBe(3); // Everything + 2 genres
    expect(el(h.doc, "scoreList").children.length).toBe(4);
    expect(el(h.doc, "readyList").children.length).toBe(2);
    expect(el(h.doc, "fsheetNote").textContent).toBe("");
  });

  it("selects a genre and reports the change", () => {
    const h = harness();
    h.sheet.paint();
    // Everything, then Anime, then Family.
    el(h.doc, "genreList").children[1]!.click();
    expect(h.model.genre).toBe("Anime");
    expect(h.changes).toBe(1);
  });

  it("hides facets and explains an unprepared library", () => {
    const h = harness([{ id: "x", title: "Raw" }]);
    h.sheet.paint();
    expect(el(h.doc, "genreHead").style.display).toBe("none");
    expect(el(h.doc, "scoreHead").style.display).toBe("none");
    expect(el(h.doc, "readyHead").style.display).toBe("none");
    expect(el(h.doc, "fsheetNote").textContent).toMatch(/hasn't been prepared/);
  });

  it("clears filters", () => {
    const h = harness();
    h.model.setGenre("Anime");
    h.sheet.paint();
    el(h.doc, "filterClear").click();
    expect(h.model.genre).toBe("");
  });
});
