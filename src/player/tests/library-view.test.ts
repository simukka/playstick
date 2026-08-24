import { describe, it, expect } from "vitest";
import { makeShell } from "./harness/page";
import type { LibraryItem } from "../src/types";

// The real view, wired to the real funnel it lights: both come out of the shell
// exactly as main.ts builds them.
function view() {
  const plays: string[] = [];
  const shell = makeShell(undefined, plays);
  return { doc: shell.doc, model: shell.model, v: shell.library, plays };
}

const items: LibraryItem[] = [
  { id: "a", title: "Akira", genres: ["Anime"], audio_langs: ["jpn"] },
  { id: "b", title: "Brave", genres: ["Family"] },
];

describe("LibraryView", () => {
  it("renders tiles and hides the empty message", () => {
    const { doc, model, v } = view();
    model.setItems(items);
    v.render();
    expect(doc.getElementById("grid")!.children).toHaveLength(2);
    expect(doc.getElementById("empty")!.hidden).toBe(true);
  });

  it("shows a filter-empty message and the way back", () => {
    const { doc, model, v } = view();
    model.setItems(items);
    model.setReady(true); // only Akira has audio; then filter to a genre with none
    model.setGenre("Family");
    v.render();
    expect(doc.getElementById("grid")!.children).toHaveLength(0);
    expect(doc.getElementById("empty")!.textContent).toMatch(/Nothing matches/);
    expect(doc.getElementById("emptyClear")!.classList.contains("on")).toBe(true);
  });

  it("shows a plain empty message when the library itself is empty", () => {
    const { doc, model, v } = view();
    model.setItems([]);
    v.render();
    expect(doc.getElementById("empty")!.textContent).toMatch(/No movies found/);
    expect(doc.getElementById("emptyClear")!.classList.contains("on")).toBe(false);
  });

  it("paints the filter chip and lights the funnel", () => {
    const { doc, model, v } = view();
    model.setItems(items);
    model.setGenre("Anime");
    model.setReady(true);
    v.render();
    const chip = doc.getElementById("filterChip")!;
    expect(chip.classList.contains("on")).toBe(true);
    expect(chip.children[0]!.textContent).toBe("Anime \u00b7 headphones");
    expect(doc.getElementById("filterBtn")!.classList.contains("live")).toBe(true);
  });

  it("says so when the movies cannot be reached", () => {
    const { doc, v } = view();
    v.unavailable();
    expect(doc.getElementById("empty")!.hidden).toBe(false);
    expect(doc.getElementById("empty")!.textContent).toMatch(/Can't reach/);
  });

  it("plays the film a tile was tapped for", () => {
    const { doc, model, v, plays } = view();
    model.setItems(items);
    v.render();
    doc.getElementById("grid")!.children[0]!.click();
    expect(plays).toEqual(["a"]);
  });
});
