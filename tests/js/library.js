// The grid's filters and sort orders.
//
// These decide which films a child can see, so the things worth holding are:
// the default shows everything in exactly the order the server sent, every
// predicate keeps the films it should and no others, a film whose metadata is
// missing lands somewhere defensible rather than being silently buried, a
// choice survives a reload, and no combination can leave the grid empty with no
// way back out of it.
//
// The script under test is the one that ships -- see tests/js/page.js.
const { install, load, check, done } = require("./page.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["library", "libraryGenres", "libraryLangs", "filtGenre",
  "filtScore", "filtReady", "filtSort", "thumbState", "tileImgs",
  "SORTS", "SCORES"];
const FNS = ["filtersOn", "matches", "visibleItems", "renderLibrary",
  "refreshThumbs", "libraryFacets", "applyFilters", "clearFilters",
  "chipText", "paintChip", "paintFilters", "showFilters", "filtersShown",
  "showSheet", "sheetOpen", "showView", "scoreOf"];

install("");
const P = load(PAGE, VARS, FNS);

// One small library that exercises every axis at once: a film with no metadata
// at all (the walk case), one with a score but no year, and two that share a
// genre. Server order is the shelf order the daemon sends, i.e. by sort_title.
const FILMS = [
  { id: "a", title: "Arrietty", sort_title: "arrietty", year: 2010,
    rating: 7.6, genres: ["Animation", "Family"], audio_langs: ["eng", "jpn"],
    has_thumb: true },
  { id: "b", title: "The Iron Giant", sort_title: "iron giant", year: 1999,
    rating: 8.0, genres: ["Animation", "Sci-Fi"], audio_langs: [],
    has_thumb: false },
  { id: "c", title: "Paddington", sort_title: "paddington", year: 2014,
    rating: 5.5, genres: ["Family"], audio_langs: ["eng"], has_thumb: false },
  { id: "d", title: "Home Movie 2003", sort_title: "", year: null,
    rating: null, genres: [], audio_langs: [], has_thumb: false },
];

function reset(items) {
  P.library = (items || FILMS).map((f) => Object.assign({}, f));
  P.filtGenre = "";
  P.filtScore = 0;
  P.filtReady = false;
  P.filtSort = "name";
  P.libraryFacets();
}
const ids = () => P.visibleItems().map((i) => i.id).join("");

reset();

// 1. The default changes nothing.

check("the default order is the order the server sent", ids() === "abcd", ids());
check("nothing is filtered out to begin with", !P.filtersOn());
check("the facets are every genre in the library, sorted",
  P.libraryGenres.join(",") === "Animation,Family,Sci-Fi", P.libraryGenres.join(","));
check("the audio languages are still derived alongside them",
  P.libraryLangs.join(",") === "eng,jpn", P.libraryLangs.join(","));

// 2. Each predicate on its own.

P.filtGenre = "Family";
check("a genre keeps only the films carrying it", ids() === "ac", ids());
check("a genre counts as a filter", P.filtersOn());
reset();

P.filtScore = 7;
check("a score threshold keeps the films at or above it", ids() === "ab", ids());
P.filtScore = 8;
check("the threshold is inclusive", ids() === "b", ids());
check("an unrated film is dropped once a threshold is asked for",
  P.visibleItems().every((i) => i.id !== "d"));
reset();

P.filtReady = true;
check("ready-for-headphones keeps only films with extracted audio",
  ids() === "ac", ids());
reset();

// 3. Combined, and the empty case.

P.filtGenre = "Animation";
P.filtScore = 7;
P.filtReady = true;
check("the three filters intersect rather than accumulate", ids() === "a", ids());
P.filtScore = 8;
check("a combination that matches nothing yields nothing", ids() === "", ids());
check("...and the chip still says what is on",
  P.chipText() === "Animation · 8+ · headphones", P.chipText());
P.renderLibrary();
check("an empty grid caused by a filter offers the way out",
  document.getElementById("emptyClear").classList.contains("on"));
check("...and says so rather than claiming the library is empty",
  document.getElementById("empty").textContent === "Nothing matches what you picked.",
  document.getElementById("empty").textContent);
P.clearFilters();
check("clearing brings every film back", ids() === "abcd", ids());
check("...and takes the escape hatch away again",
  !document.getElementById("emptyClear").classList.contains("on"));
check("...and the chip with it", !document.getElementById("filterChip").classList.contains("on"));

// A library that is genuinely empty must not be blamed on the filters.
reset([]);
P.renderLibrary();
check("an empty library is reported as an empty library",
  document.getElementById("empty").textContent === "No movies found yet.",
  document.getElementById("empty").textContent);
check("...with no clear button, because clearing would not help",
  !document.getElementById("emptyClear").classList.contains("on"));
reset();

// 4. Sorting, and where the film with no year goes.

P.filtSort = "year-desc";
check("newest first", ids() === "cab" + "d", ids());
P.filtSort = "year-asc";
check("oldest first", ids() === "bac" + "d", ids());
check("a film with no year is last in BOTH year orders, not oldest",
  P.visibleItems()[3].id === "d");
P.filtSort = "name";
check("the name order is the server's own order, untouched", ids() === "abcd", ids());
check("sorting is not a filter", !P.filtersOn());

// The shelf key, not the raw title: "The Iron Giant" files under I. The server
// sends that order, so the check that matters is that the page does not undo it.
check("the page does not re-sort 'The' back to the front",
  P.visibleItems()[1].id === "b", ids());

// Two films in the same year fall back to the shelf key rather than to
// whichever the sort happened to touch first.
reset([
  { id: "z", title: "Zootropolis", sort_title: "zootropolis", year: 2016,
    genres: [], audio_langs: [] },
  { id: "m", title: "Moana", sort_title: "moana", year: 2016,
    genres: [], audio_langs: [] },
]);
P.filtSort = "year-desc";
check("films sharing a year fall back to the shelf order", ids() === "mz", ids());
reset();

// 5. Persistence, and a filter that outlives its films.

P.filtGenre = "Sci-Fi";
P.filtScore = 7;
P.filtReady = true;
P.filtSort = "year-asc";
P.applyFilters();
check("the choice is written down", localStorage.getItem("ps.lib.genre") === "Sci-Fi" &&
  localStorage.getItem("ps.lib.score") === "7" &&
  localStorage.getItem("ps.lib.ready") === "1" &&
  localStorage.getItem("ps.lib.sort") === "year-asc");
P.clearFilters();
check("clearing removes the keys rather than storing an empty filter",
  localStorage.getItem("ps.lib.genre") === null &&
  localStorage.getItem("ps.lib.ready") === null);
check("...but keeps the sort, which hides nothing",
  localStorage.getItem("ps.lib.sort") === "year-asc");

// A re-prepped library that no longer has the genre somebody left selected.
reset();
P.filtGenre = "Sci-Fi";
P.library = FILMS.filter((f) => f.id !== "b").map((f) => Object.assign({}, f));
P.libraryFacets();
check("a genre that has left the library stops filtering", P.filtGenre === "");
check("...so the grid is not left mysteriously empty", ids() === "acd", ids());
reset();

// 6. Thumbnails, which used to be swapped by position.

P.filtGenre = "Family";
P.renderLibrary();
check("only the matching tiles are built",
  Object.keys(P.tileImgs).sort().join("") === "ac",
  Object.keys(P.tileImgs).sort().join(""));
// The server now says Paddington has a poster. Under the old positional swap
// this would have repainted whichever tile happened to sit at index 2.
const fresh = P.library.map((f) => Object.assign({}, f,
  f.id === "c" ? { has_thumb: true } : {}));
P.refreshThumbs(fresh);
check("a new poster lands on its own tile, not on the one at its index",
  /^\/api\/thumb\/c\?t=/.test(P.tileImgs.c.src), P.tileImgs.c.src);
check("...and no other tile was touched",
  P.tileImgs.a.src === "/api/thumb/a", P.tileImgs.a.src);
check("a film filtered off the grid is still tracked without throwing",
  P.thumbState.b === false);
reset();

// 7. The sheet only offers what the library can answer.

P.showFilters(true);
check("the sheet paints", P.filtersShown());
const kinds = document.getElementById("genreList").children.length;
check("the genre list is 'Everything' plus one row per genre", kinds === 4, String(kinds));
check("all three year orders are offered when years are known",
  document.getElementById("sortList").children.length === 3);
check("the score section is shown when anything carries a score",
  document.getElementById("scoreHead").style.display === "");

// An unprepped library: titles from filenames and nothing else.
reset([
  { id: "p", title: "Ponyo", genres: [], audio_langs: [] },
  { id: "q", title: "Totoro", genres: [], audio_langs: [] },
]);
P.paintFilters();
check("an unprepped library hides the genre section",
  document.getElementById("genreHead").style.display === "none");
check("...and the score section",
  document.getElementById("scoreHead").style.display === "none");
check("...and the headphone section",
  document.getElementById("readyHead").style.display === "none");
check("...and offers only the name order",
  document.getElementById("sortList").children.length === 1,
  String(document.getElementById("sortList").children.length));
check("...and says why instead of showing four empty lists",
  /hasn't been prepared/.test(document.getElementById("fsheetNote").textContent),
  document.getElementById("fsheetNote").textContent);

// 8. The two sheets share a scrim and never stack.

reset();
P.showFilters(true);
check("opening the filters shows the scrim",
  document.getElementById("sheetScrim").classList.contains("on"));
P.showSheet(true);
check("opening the sound sheet closes the filter sheet", !P.filtersShown());
check("...and the scrim stays up for the one that is open",
  document.getElementById("sheetScrim").classList.contains("on"));
P.showFilters(true);
check("and the other way round",
  P.filtersShown() && !document.getElementById("sheet").classList.contains("on"));
P.showFilters(false);
check("closing the last sheet takes the scrim down",
  !document.getElementById("sheetScrim").classList.contains("on"));

// 9. The funnel is a grid control.

P.showView("playing");
check("the funnel is gone while a film is playing",
  !document.getElementById("filterBtn").classList.contains("on"));
P.showView("library");
check("...and back on the grid",
  document.getElementById("filterBtn").classList.contains("on"));
P.filtGenre = "Family";
P.paintChip();
check("the funnel is lit while a filter is on",
  document.getElementById("filterBtn").classList.contains("live"));

done();
