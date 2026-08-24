import { bench, describe } from "vitest";
import { Grid, type TileItem } from "../src/dom";
import { FakeDocument, FakeEl, asDocument, asEl } from "./harness/dom";

function make(n: number, shuffle = 0): TileItem[] {
  const items: TileItem[] = [];
  for (let i = 0; i < n; i++) {
    items.push({ id: String(i), title: "Film " + i, has_thumb: true });
  }
  for (let s = 0; s < shuffle; s++) {
    const a = s % n;
    const b = (s * 7 + 3) % n;
    const tmp = items[a]!;
    items[a] = items[b]!;
    items[b] = tmp;
  }
  return items;
}

function newGrid() {
  const doc = new FakeDocument();
  const parent = new FakeEl("div");
  return new Grid(asDocument(doc), asEl(parent), (p) => p, () => 0);
}

// The two updates that happen in service: a poll that changed nothing (the
// common case, must be free) and a poster-arrival / small-reorder. The stub's
// costs are not a browser's, but the reconciler's own work -- the Map lookups,
// the order checks, the moves it decides to make -- is exactly what these
// measure, and a regression to clear-and-rebuild shows up as a cliff.
describe("Grid.update", () => {
  bench("unchanged poll over 300 tiles (should be near free)", () => {
    const g = newGrid();
    const items = make(300);
    g.update(items);
    for (let k = 0; k < 2000; k++) {
      g.update(items);
    }
  });

  bench("light reorder over 300 tiles", () => {
    const g = newGrid();
    g.update(make(300));
    for (let k = 0; k < 500; k++) {
      g.update(make(300, 6));
    }
  });
});
