// The one test that runs the page the way a phone does.
//
// Everything else here drives a module against the hand-rolled stub, which is
// fast and enough for logic -- but the stub cannot tell you that the markup the
// views build is markup a parser accepts, that the stylesheet reaches them, or
// that boot() gets all the way through against a real window. Those are exactly
// the failures the move of the page into TypeScript could cause, and they are
// silent: a page that throws on line one still serves, still returns 200, and
// shows a child a black screen.
//
// So this one builds the real page -- via the real build, so it cannot drift
// from what ships -- and boots it in jsdom. It is the slowest test in the suite
// by an order of magnitude, and it is one test, on purpose.
import { describe, it, expect, beforeAll, afterEach } from "vitest";
import { JSDOM } from "jsdom";
// @ts-expect-error -- the build is plain ESM with no types; this is its only caller.
import { renderPage } from "../build.mjs";
import type { Status } from "../src/types";

let PAGE = "";

beforeAll(async () => {
  const built = (await renderPage()) as { page: string };
  PAGE = built.page;
}, 60_000);

const ITEMS = [
  { id: "a", title: "Akira", year: 1988, rating: "8.0", genres: ["Anime"], audio_langs: ["jpn"], has_thumb: true },
  { id: "b", title: "Brave", year: 2012, rating: "7.1", genres: ["Family"] },
];

interface Page {
  dom: JSDOM;
  doc: Document;
  win: Window & typeof globalThis;
  el(id: string): HTMLElement;
  css(id: string): CSSStyleDeclaration;
  tap(id: string): void;
  sheet: string;
}

let open: JSDOM | null = null;

// jsdom keeps the page's intervals running, and vitest will not exit while they
// do. Every test closes its window.
afterEach(() => {
  open?.window.close();
  open = null;
});

/** Boot the page against a daemon that reports `status`, and wait for the first
 * poll to land. */
async function boot(status: Partial<Status> = {}, search = ""): Promise<Page> {
  const reported: Status = { state: "idle", ...status } as Status;
  const dom = new JSDOM(PAGE, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "http://localhost:8080/" + search,
    beforeParse(win) {
      // The daemon, in four lines. Nothing here is under test -- net.ts has its
      // own tests -- it only has to answer.
      (win as unknown as { fetch: unknown }).fetch = (url: string) => {
        const path = String(url).split("?")[0];
        const body =
          path === "/api/library"
            ? { available: true, items: ITEMS }
            : path === "/api/time"
              ? { t: Date.now() / 1000, session: "s" }
              : reported;
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
      };
    },
  });
  open = dom;
  const doc = dom.window.document;
  const page: Page = {
    dom,
    doc: doc as unknown as Document,
    win: dom.window as unknown as Window & typeof globalThis,
    el: (id) => doc.getElementById(id) as unknown as HTMLElement,
    css: (id) => dom.window.getComputedStyle(doc.getElementById(id)!) as unknown as CSSStyleDeclaration,
    tap: (id) =>
      doc.getElementById(id)!.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })),
    sheet: "",
  };
  // The poll goes out at boot; wait for what it paints rather than for a clock.
  await until(() => !!page.el("grid") && page.el("grid").children.length > 0);
  page.sheet = doc.head.querySelector("style")?.textContent ?? "";
  return page;
}

async function until(cond: () => boolean, ms = 3000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > deadline) {
      throw new Error("timed out waiting for the page");
    }
    await new Promise((r) => setTimeout(r, 5));
  }
}

// Every id the views resolve or a test reaches for. A view that stops building
// one of these is the failure this file exists to catch.
const IDS = [
  "banner", "audioBtn", "filterBtn", "tapToListen", "sheetScrim", "sheet", "fsheet",
  "library", "grid", "empty", "emptyClear", "filterChip", "preparing", "prepArt",
  "prepTitle", "prepStep", "prepBar", "prepCancel", "playing", "nowTitle", "nowSub",
  "bar", "barFill", "toggle", "stop", "volume", "volDown", "volUp", "destList",
  "langList", "sheetNote", "syncHead", "syncRow", "syncBack", "syncVal", "syncFwd",
  "syncHint", "syncDebug", "tuneHead", "tuneList", "tuneFoot", "tuneReset", "tuneNote",
  "sortList", "genreHead", "genreList", "scoreHead", "scoreList", "readyHead",
  "readyList", "fsheetNote", "filterClear",
];

describe("the page boots", () => {
  it("builds every element the views resolve", async () => {
    const p = await boot();
    expect(IDS.filter((id) => !p.doc.getElementById(id))).toEqual([]);
  });

  it("hangs them off the body in the order the template had", async () => {
    const p = await boot();
    const order = [...p.doc.body.children]
      .filter((e) => e.tagName !== "SCRIPT")
      .map((e) => e.id);
    expect(order).toEqual([
      "banner", "audioBtn", "filterBtn", "tapToListen", "sheetScrim",
      "sheet", "fsheet", "library", "preparing", "playing",
    ]);
  });

  it("leaves nothing of the curator view behind", async () => {
    const p = await boot();
    expect(p.doc.getElementById("asheet")).toBeNull();
    expect(p.doc.getElementById("adminBadge")).toBeNull();
    expect(p.sheet).not.toContain(".editBtn");
  });

  it("installs one stylesheet, and it applies", async () => {
    const p = await boot();
    expect(p.doc.head.querySelectorAll("style")).toHaveLength(1);
    // The tokens are asserted as text: jsdom computes styles but does not
    // resolve var(), so a rule painted from one reads as empty here.
    expect(p.sheet).toContain("--accent: #4da3ff;");
    expect(p.sheet).toMatch(/#stop \{[^}]*background: var\(--stop\)/);
    expect(p.sheet).toMatch(/#toggle \{[^}]*background: var\(--accent\)/);
    // Everything else is a real computed value off a real cascade.
    expect(p.css("grid").display).toBe("grid");
    expect(p.css("sheet").backgroundColor).toBe("rgb(27, 27, 33)");
    expect(p.css("sheetScrim").backgroundColor).toBe("rgba(0, 0, 0, 0.6)");
    expect(p.css("bar").backgroundColor).toBe("rgb(38, 38, 46)");
  });

  it("shows the grid and hides the other two views", async () => {
    const p = await boot();
    expect(p.css("library").display).toBe("block");
    expect(p.css("preparing").display).toBe("none");
    expect(p.css("playing").display).toBe("none");
    expect(p.css("sheet").display).toBe("none");
    expect(p.css("sheetScrim").display).toBe("none");
    expect(p.css("tapToListen").display).toBe("none");
  });

  it("draws the library it was served", async () => {
    const p = await boot();
    const tiles = p.el("grid").children;
    expect(tiles).toHaveLength(2);
    expect(tiles[0]!.querySelector("span")!.textContent).toBe("Akira");
    expect(tiles[0]!.querySelector("img")!.getAttribute("src")).toMatch(/\/api\/thumb\/a/);
    expect((p.el("empty") as HTMLElement).hidden).toBe(true);
  });

  it("puts the icons in the round buttons", async () => {
    const p = await boot();
    expect(p.el("filterBtn").querySelector("svg")).not.toBeNull();
    expect(p.el("audioBtn").querySelector("svg")).not.toBeNull();
    expect(p.css("filterBtn").display).toBe("inline-flex");
  });
});

describe("the sheets, from a real tap", () => {
  it("opens the filter sheet over the scrim and paints its rows", async () => {
    const p = await boot();
    p.tap("filterBtn");
    expect(p.css("fsheet").display).toBe("block");
    expect(p.css("sheetScrim").display).toBe("block");
    expect(p.el("sortList").children).toHaveLength(3);
    expect(p.el("genreList").children).toHaveLength(3); // Everything + 2 kinds
  });

  it("filters the grid, says so on the chip, and clears from the chip", async () => {
    const p = await boot();
    p.tap("filterBtn");
    (p.el("genreList").children[1] as HTMLElement).click();
    expect(p.el("grid").children).toHaveLength(1);
    expect(p.css("filterChip").display).toBe("flex");
    expect(p.el("filterChip").textContent).toMatch(/^Anime/);
    expect(p.el("filterBtn").classList.contains("live")).toBe(true);

    p.tap("filterChip");
    expect(p.el("grid").children).toHaveLength(2);
    expect(p.css("filterChip").display).toBe("none");
  });

  it("keeps one sheet up at a time, and the scrim shuts it", async () => {
    const p = await boot();
    p.tap("filterBtn");
    p.tap("audioBtn");
    expect(p.css("sheet").display).toBe("block");
    expect(p.css("fsheet").display).toBe("none");
    expect(p.el("destList").children).toHaveLength(2);

    p.tap("sheetScrim");
    expect(p.css("sheet").display).toBe("none");
    expect(p.css("sheetScrim").display).toBe("none");
  });

  it("renders the playback parameters only under ?debug", async () => {
    const plain = await boot({}, "?x=1");
    plain.tap("audioBtn");
    expect(plain.css("tuneList").display).toBe("none");
    plain.win.close();

    const p = await boot({}, "?debug");
    p.tap("audioBtn");
    expect(p.css("tuneList").display).toBe("block");
    expect(p.el("tuneList").children.length).toBeGreaterThan(0);
  });
});

describe("the views a film brings up", () => {
  it("shows the film, the time left and the controls while playing", async () => {
    const p = await boot({
      state: "playing",
      id: "a",
      title: "Akira",
      duration: 7200,
      position: 1800,
      audio: true,
    });
    await until(() => p.css("playing").display === "block");
    expect(p.css("library").display).toBe("none");
    // No funnel off the grid: there is no list up here to narrow.
    expect(p.css("filterBtn").display).toBe("none");
    expect(p.el("nowTitle").textContent).toBe("Akira");
    expect(p.el("nowSub").textContent).toBe("1 h 30 min left");
    expect(p.el("barFill").style.width).toBe("25%");
    expect(p.el("toggle").innerHTML).toBe("❙❙");
    expect(p.css("volume").display).toBe("flex");
  });

  it("shows the poster and the daemon's own words while getting ready", async () => {
    const p = await boot({
      state: "preparing",
      id: "a",
      title: "Akira",
      prepare: { label: "Waiting for the lamp…" },
    } as Partial<Status>);
    await until(() => p.css("preparing").display === "block");
    expect(p.el("prepTitle").textContent).toBe("Akira");
    expect(p.el("prepStep").textContent).toBe("Waiting for the lamp…");
    expect(p.el("prepArt").getAttribute("src")).toMatch(/\/api\/thumb\/a/);
    expect(p.el("prepBar").children).toHaveLength(1); // the sweeping fill
  });
});
