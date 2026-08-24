import { bench, describe } from "vitest";
import { LibraryModel } from "../src/library";
import type { LibraryItem } from "../src/types";

const GENRES = ["Anime", "Family", "SciFi", "Drama", "Comedy", "Docs"];

function bigLibrary(n: number): LibraryItem[] {
  const items: LibraryItem[] = [];
  for (let i = 0; i < n; i++) {
    items.push({
      id: String(i),
      title: "Film " + i,
      year: 1980 + (i % 45),
      rating: String(5 + (i % 5)),
      genres: [GENRES[i % GENRES.length]!],
      audio_langs: i % 2 ? ["eng"] : [],
      hidden: i % 17 === 0,
    });
  }
  return items;
}

// A real library is a few hundred films, but the filter runs on every genre or
// score tap and must feel instant on a phone. Benched at 5000 to leave headroom
// and to make an accidental O(n^2) obvious. Memoisation means the poll path is
// free; this measures the invalidated path.
describe("LibraryModel.visible", () => {
  bench("filter + year-sort a 5000-film library, invalidated each time", () => {
    const m = new LibraryModel();
    m.setItems(bigLibrary(5000));
    m.setSort("year-desc");
    for (let k = 0; k < 200; k++) {
      m.setGenre(GENRES[k % GENRES.length]!);
      m.visible();
    }
  });

  bench("setItems facet derivation, 5000 films", () => {
    const m = new LibraryModel();
    const lib = bigLibrary(5000);
    for (let k = 0; k < 200; k++) {
      m.setItems(lib);
    }
  });
});
