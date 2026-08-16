// The page, for tests.
//
// The views build their own markup now, so there is no list of ids to keep in
// step with a template: a test constructs the real module and the elements
// appear, indexed by the ids the browser would resolve. What is left here is the
// shell -- the parts of the page a view under test does not build itself, put
// together from the same modules main.ts uses.
import { FakeDocument, asDocument } from "./dom";
import { h } from "../../src/dom";
import { LibraryModel } from "../../src/library";
import { LibraryView } from "../../src/library-view";
import { SheetManager } from "../../src/sheet";

/** An empty document. Everything a test needs in it, the code under test builds. */
export function makePage(): FakeDocument {
  return new FakeDocument();
}

export interface Shell {
  doc: FakeDocument;
  model: LibraryModel;
  sheets: SheetManager;
  library: LibraryView;
  /** Ready to spread into a StatusPresenter's deps. */
  parts: { library: HTMLElement; grid: HTMLElement; filterBtn: HTMLElement };
  soundPaints: () => number;
  filterPaints: () => number;
}

/**
 * The grid view and the sheet chrome, wired to each other as main.ts wires them.
 * The two sheets are stand-ins: the manager only ever toggles a class on them,
 * and each real sheet is exercised by its own test.
 */
export function makeShell(doc: FakeDocument = makePage(), plays: string[] = []): Shell {
  const d = asDocument(doc);
  const model = new LibraryModel();
  let soundPaints = 0;
  let filterPaints = 0;

  const sheets = new SheetManager(d, {
    sound: h(d, "div", { id: "sheet", class: "sheet" }),
    filter: h(d, "div", { id: "fsheet", class: "sheet" }),
    onSoundOpen: () => soundPaints++,
    onFilterOpen: () => filterPaints++,
  });
  const library = new LibraryView(
    d,
    model,
    (p) => p + "?v=b",
    (id) => plays.push(id),
    sheets.filterBtn,
    () => 0,
  );

  return {
    doc,
    model,
    sheets,
    library,
    parts: {
      library: library.root,
      grid: library.gridEl,
      filterBtn: sheets.filterBtn,
    },
    soundPaints: () => soundPaints,
    filterPaints: () => filterPaints,
  };
}
