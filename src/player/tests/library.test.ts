import { describe, it, expect } from "vitest";
import { LibraryModel } from "../src/library";
import type { LibraryItem } from "../src/types";

function item(over: Partial<LibraryItem>): LibraryItem {
  return { id: over.id ?? "x", title: over.title ?? "Untitled", ...over };
}

const SAMPLE: LibraryItem[] = [
  item({ id: "a", title: "Akira", year: 1988, rating: "8.0", genres: ["Anime"], audio_langs: ["jpn"] }),
  item({ id: "b", title: "Brave", year: 2012, rating: "7.1", genres: ["Family"], audio_langs: [] }),
  item({ id: "c", title: "The Cat", sort_title: "Cat, The", year: 2001, rating: "6.5", genres: ["Family", "Anime"] }),
  item({ id: "d", title: "Dune", year: 2021, rating: "cert:PG", genres: ["SciFi"], audio_langs: ["eng", "fin"] }),
  item({ id: "e", title: "Echo", genres: ["SciFi"], hidden: true }),
];

describe("LibraryModel filtering", () => {
  it("drops hidden films with no curator view", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    expect(m.visible().map((i) => i.id)).not.toContain("e");
  });

  it("filters by genre", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    m.setGenre("Anime");
    expect(m.visible().map((i) => i.id).sort()).toEqual(["a", "c"]);
  });

  it("filters by minimum score, treating a certification as unrated", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    m.setMinScore(7);
    // a (8.0) and b (7.1) pass; c (6.5) fails; d ("cert:PG" -> NaN) is unrated.
    expect(m.visible().map((i) => i.id).sort()).toEqual(["a", "b"]);
  });

  it("filters by headphone-readiness", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    m.setReady(true);
    expect(m.visible().map((i) => i.id).sort()).toEqual(["a", "d"]);
  });

  it("combines filters", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    m.setGenre("Anime");
    m.setReady(true);
    expect(m.visible().map((i) => i.id)).toEqual(["a"]);
  });

  it("reports whether any filter is on", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    expect(m.filtersOn()).toBe(false);
    m.setReady(true);
    expect(m.filtersOn()).toBe(true);
    m.clearFilters();
    expect(m.filtersOn()).toBe(false);
  });
});

describe("LibraryModel sorting", () => {
  it("keeps the server order under name sort", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    expect(m.visible().map((i) => i.id)).toEqual(["a", "b", "c", "d"]);
  });

  it("sorts newest and oldest first, unknown years last in both", () => {
    const m = new LibraryModel();
    m.setItems([
      item({ id: "old", year: 1990, title: "Old" }),
      item({ id: "new", year: 2020, title: "New" }),
      item({ id: "none", title: "Unknown" }),
    ]);
    m.setSort("year-desc");
    expect(m.visible().map((i) => i.id)).toEqual(["new", "old", "none"]);
    m.setSort("year-asc");
    expect(m.visible().map((i) => i.id)).toEqual(["old", "new", "none"]);
  });

  it("breaks year ties by shelf title", () => {
    const m = new LibraryModel();
    m.setItems([
      item({ id: "z", title: "Zed", year: 2000 }),
      item({ id: "a", title: "The Ant", sort_title: "Ant, The", year: 2000 }),
    ]);
    m.setSort("year-asc");
    expect(m.visible().map((i) => i.id)).toEqual(["a", "z"]);
  });
});

describe("LibraryModel facets", () => {
  it("derives sorted genres and languages", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    expect(m.facets.genres).toEqual(["Anime", "Family", "SciFi"]);
    expect(m.facets.langs).toEqual(["eng", "fin", "jpn"]);
  });

  it("refuses a genre no film has", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    m.setGenre("Horror");
    expect(m.genre).toBe("");
  });

  it("drops a live genre filter when a reload removes its films", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    m.setGenre("Anime");
    expect(m.genre).toBe("Anime");
    m.setItems([item({ id: "d", genres: ["SciFi"] })]);
    expect(m.genre).toBe("");
  });
});

describe("LibraryModel memoisation", () => {
  it("returns the same array until something invalidates it", () => {
    const m = new LibraryModel();
    m.setItems(SAMPLE);
    const first = m.visible();
    expect(m.visible()).toBe(first); // no work on an unchanged poll
    m.setGenre("Anime");
    expect(m.visible()).not.toBe(first);
  });
});

describe("LibraryModel performance", () => {
  // Backs tests/library.bench.ts. Filtering runs on every filter tap; this
  // catches an accidental O(n^2) or per-item allocation on a big library.
  it("filters and sorts 5000 films under budget", () => {
    const items: LibraryItem[] = [];
    for (let i = 0; i < 5000; i++) {
      items.push(
        item({
          id: String(i),
          title: "Film " + i,
          year: 1980 + (i % 45),
          rating: String(5 + (i % 5)),
          genres: [["Anime", "Family", "SciFi"][i % 3]!],
        }),
      );
    }
    const m = new LibraryModel();
    m.setItems(items);
    m.setSort("year-desc");
    const t0 = performance.now();
    for (let k = 0; k < 300; k++) {
      m.setGenre(["Anime", "Family", "SciFi"][k % 3]!);
      m.visible();
    }
    const ms = performance.now() - t0;
    expect(ms).toBeLessThan(800);
  });
});
