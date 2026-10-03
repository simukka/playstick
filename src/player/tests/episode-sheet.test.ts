import { describe, it, expect } from "vitest";
import { EpisodeSheet, episodeName, seasonName } from "../src/episode-sheet";
import { LibraryModel, SHOW_PREFIX, fullTitle } from "../src/library";
import { makePage } from "./harness/page";
import { asDocument, FakeEl } from "./harness/dom";
import type { LibraryItem } from "../src/types";

function ep(id: string, season: number, episode: number, over: Partial<LibraryItem> = {}): LibraryItem {
  return {
    id,
    title: over.title ?? "Episode " + episode,
    series: "Bluey",
    season,
    episode,
    sort_title: `bluey ${String(season).padStart(3, "0")} ${String(episode).padStart(4, "0")}`,
    ...over,
  };
}

// In the daemon's shelf order, which is the order the rows arrive in.
const LIBRARY: LibraryItem[] = [
  { id: "a", title: "Akira", sort_title: "akira", year: 1988, genres: ["Anime"] },
  ep("s2e1", 2, 1, { title: "Dance Mode" }),
  ep("s1e2", 1, 2, { title: "Hospital", has_thumb: true }),
  ep("s1e1", 1, 1, { title: "Magic Xylophone", year: 2018, genres: ["Family"] }),
  ep("sp1", 0, 1, { title: "Christmas" }),
  ep("hid", 1, 3, { hidden: true }),
  { id: "p", title: "Ponyo", sort_title: "ponyo", year: 2008, genres: ["Family"] },
];

function model(items = LIBRARY) {
  const m = new LibraryModel();
  m.setItems(items);
  return m;
}

describe("LibraryModel folding episodes", () => {
  it("shows a series as one tile, where the series sits on the shelf", () => {
    const tiles = model().visible();
    expect(tiles.map((t) => t.id)).toEqual(["a", SHOW_PREFIX + "bluey", "p"]);
    expect(tiles[1]!.title).toBe("Bluey");
  });

  it("borrows the first episode's poster for the tile", () => {
    const tile = model().visible()[1]!;
    expect(tile.thumb).toBe("s1e1");
  });

  it("orders episodes by season then number, specials last, hidden left out", () => {
    const show = model().show(SHOW_PREFIX + "bluey")!;
    expect(show.episodes.map((e) => e.id)).toEqual(["s1e1", "s1e2", "s2e1", "sp1"]);
    expect(show.seasons).toEqual([1, 2, 0]);
  });

  it("keeps the tile while any episode passes the filter, and counts it once", () => {
    const m = model();
    m.setGenre("Family");
    expect(m.visible().map((t) => t.id)).toEqual([SHOW_PREFIX + "bluey", "p"]);
    expect(m.count((i) => (i.genres || []).indexOf("Family") >= 0)).toBe(2);
  });

  it("is not a show for a film's id", () => {
    expect(model().show("a")).toBeUndefined();
  });

  it("treats a series named with different case as the same show", () => {
    const m = model([ep("x", 1, 1), ep("y", 1, 2, { series: "bluey " })]);
    expect(m.visible().length).toBe(1);
    expect(m.show(SHOW_PREFIX + "bluey")!.episodes.length).toBe(2);
  });

  it("names an episode by its series for the playing screen", () => {
    expect(fullTitle(ep("x", 1, 2, { title: "Hospital" }))).toBe("Bluey S1 E2 · Hospital");
    expect(fullTitle({ id: "a", title: "Akira" })).toBe("Akira");
  });
});

describe("EpisodeSheet", () => {
  function harness() {
    const doc = makePage();
    const m = model();
    const picked: string[] = [];
    const sheet = new EpisodeSheet(asDocument(doc), (id) => picked.push(id));
    sheet.load(m.show(SHOW_PREFIX + "bluey")!);
    sheet.paint();
    const el = (id: string) => doc.getElementById(id) as unknown as FakeEl;
    return { doc, sheet, picked, el };
  }

  it("opens on the first season with the show's name", () => {
    const h = harness();
    expect(h.el("esheetTitle").textContent).toBe("Bluey");
    const seasons = h.el("esheetSeasons");
    expect(seasons.hidden).toBe(false);
    expect(seasons.children.map((b) => b.textContent)).toEqual(["Season 1", "Season 2", "Specials"]);
    expect(seasons.children[0]!.classList.contains("sel")).toBe(true);
    expect(h.el("esheetList").children.length).toBe(2);
  });

  it("switches season, and plays the episode tapped", () => {
    const h = harness();
    h.el("esheetSeasons").children[1]!.click();
    const list = h.el("esheetList");
    expect(list.children.length).toBe(1);
    list.children[0]!.click();
    expect(h.picked).toEqual(["s2e1"]);
  });

  it("does not offer a choice of one season", () => {
    const doc = makePage();
    const m = model([ep("x", 1, 1), ep("y", 1, 2)]);
    const sheet = new EpisodeSheet(asDocument(doc), () => {});
    sheet.load(m.show(SHOW_PREFIX + "bluey")!);
    sheet.paint();
    expect((doc.getElementById("esheetSeasons") as unknown as FakeEl).hidden).toBe(true);
  });

  it("names rows by number, without repeating a title that is only the number", () => {
    expect(episodeName(ep("x", 1, 2, { title: "Hospital" }))).toBe("2. Hospital");
    expect(episodeName(ep("x", 1, 3))).toBe("Episode 3");
    expect(seasonName(0)).toBe("Specials");
  });
});
