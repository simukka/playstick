// The desktop curator view.
//
// This is the one place the page stops being built for a child holding a phone
// and lets somebody edit the library instead, so the things worth holding are:
// it only turns on where it should, a hidden film leaves the children's grid
// but not the curator's, the grid grows an edit control that opens the film it
// belongs to, and a save sends exactly what the daemon coerces -- an emptied
// box included, because that is how a field is reset.
//
// The script under test is the one that ships -- see tests/js/page.js.
const { install, load, check, done } = require("./page.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["library", "adminMode", "editingId", "filtGenre", "filtScore",
  "filtReady", "filtSort", "tileImgs", "thumbState"];
const FNS = ["detectAdmin", "matches", "visibleItems", "renderLibrary",
  "openEditor", "saveEditor", "showEditor", "editorShown", "findItem",
  "libraryFacets", "post", "showSheet", "showFilters", "sheetOpen",
  "filtersShown"];

// Loaded WITH ?admin so the view is on: adminMode is read once at load, and the
// rest of the file flips the exposed flag directly to test the other side.
install("?admin");
const P = load(PAGE, VARS, FNS);

const el = (id) => document.getElementById(id);

// 1. detectAdmin only turns on where it should.

check("?admin is enough on its own", P.adminMode === true);

global.location = { search: "?admin=0", href: "x" };
check("?admin=0 forces it off even on a desktop", P.detectAdmin() === false);
global.location = { search: "?debug", href: "x" };
global.matchMedia = () => ({ matches: true, addEventListener() {} });
check("a fine, hovering pointer is the desktop tell", P.detectAdmin() === true);
global.matchMedia = () => ({ matches: false, addEventListener() {} });
check("a touchscreen gets the child's page", P.detectAdmin() === false);
global.matchMedia = undefined;
check("a browser without matchMedia does not throw, and stays off",
  P.detectAdmin() === false);

// 2. Hidden films: gone for the children, kept for the curator.

const FILMS = [
  { id: "a", title: "Arrietty", sort_title: "arrietty", genres: [],
    audio_langs: [], has_thumb: false },
  { id: "b", title: "Grave of the Fireflies", sort_title: "grave",
    genres: [], audio_langs: [], has_thumb: false, hidden: true },
  { id: "c", title: "Ponyo", sort_title: "ponyo", genres: [],
    audio_langs: [], has_thumb: false },
];
function reset() {
  P.library = FILMS.map((f) => Object.assign({}, f));
  P.filtGenre = ""; P.filtScore = 0; P.filtReady = false; P.filtSort = "name";
  P.libraryFacets();
}
const ids = () => P.visibleItems().map((i) => i.id).join("");

reset();
P.adminMode = false;
check("the children never see a hidden film", ids() === "ac", ids());
check("...and matches() is the line that drops it", P.matches(FILMS[1]) === false);
P.adminMode = true;
check("the curator sees every film, hidden ones included", ids() === "abc", ids());
check("...and matches() keeps it for them", P.matches(FILMS[1]) === true);

// 3. The grid grows an edit control, and hidden films are badged.

reset();
P.adminMode = true;
P.renderLibrary();
const grid = el("grid");
check("every visible film is a cell in the curator grid",
  grid.children.length === 3, String(grid.children.length));
check("the hidden film's cell is marked so it can be dimmed and badged",
  grid.children[1].classList.contains("hidden"));
check("...and no other cell is", !grid.children[0].classList.contains("hidden"));
const cell = grid.children[0];
check("each cell carries a play tile, a badge and an edit button",
  cell.children.length === 3 &&
  cell.children[0].classList.contains("tile") &&
  cell.children[2].classList.contains("editBtn"),
  cell.children.map((c) => c.className).join("|"));

// A phone build is unchanged: the grid children are plain tiles, no cells.
P.adminMode = false;
P.renderLibrary();
check("the child's grid is plain tiles with no edit controls",
  el("grid").children.every((c) => c.classList.contains("tile")));
P.adminMode = true;

// 4. The edit button opens the editor on its own film.

reset();
P.renderLibrary();
el("grid").children[2].children[2].click();     // Ponyo's edit button
check("tapping edit opens the editor sheet", P.editorShown());
check("...on the film it belongs to", P.editingId === "c", P.editingId);
check("...with that film's title in the box", el("aTitle").value === "Ponyo",
  el("aTitle").value);

// 5. A save sends what was typed, and an emptied box is sent as empty so the
//    daemon can read it as a reset rather than as no change.

reset();
P.renderLibrary();
P.openEditor(P.findItem("a"));
el("aTitle").value = "Arrietty";
el("aSort").value = "arrietty";
el("aYear").value = "2010";
el("aRating").value = "";               // cleared: a reset on the daemon side
el("aGenres").value = "Animation, Family";
el("aHiddenRow").classList.add("on");   // hide it

let sent = null;
global.fetch = (path, opts) => {
  sent = { path: path, body: JSON.parse(opts.body) };
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      id: "a", title: "Arrietty", sort_title: "arrietty", year: 2010,
      rating: null, genres: ["Animation", "Family"], hidden: true,
    }),
  });
};
P.saveEditor();

// The fetch above resolves on the microtask queue; let it drain before asserting.
Promise.resolve().then(() => Promise.resolve()).then(() => {
  check("the edit is posted to the admin route", sent && sent.path === "/api/admin/item",
    sent && sent.path);
  check("...naming the film being edited", sent.body.id === "a");
  check("the genres box is split into a list", sent.body.fields.title === "Arrietty" &&
    JSON.stringify(sent.body.fields.genres) === '["Animation","Family"]',
    JSON.stringify(sent.body.fields));
  check("a cleared box is sent as empty, for the daemon to read as a reset",
    sent.body.fields.rating === "");
  check("the hidden toggle rides along", sent.body.fields.hidden === true);

  // The daemon's answer, not what was typed, is what the grid takes on.
  const film = P.findItem("a");
  check("the film is updated from the daemon's answer", film.hidden === true &&
    film.year === 2010, JSON.stringify(film));
  check("...and the editor closes on success", !P.editorShown());

  done();
});
