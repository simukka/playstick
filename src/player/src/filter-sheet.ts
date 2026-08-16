// The filter sheet: sort order, and the three facets a prepared library offers
// -- kind, score, headphone-readiness. A heading with nothing under it reads as
// a list that failed to load, so each facet with no values is taken away
// entirely and a note says why.
import { h, head, grip, setText } from "./dom";
import { row } from "./sheet";
import type { LibraryModel, SortKey } from "./library";
import type { LibraryItem } from "./types";

export const FILTER_CSS = `
#fsheetNote { color: var(--dim); font-size: 16px; line-height: 1.45; padding: 6px 14px 2px; }
#filterClear {
  display: block; width: 100%; margin: 14px 0 2px; min-height: 56px;
  border-radius: 14px; background: var(--card); font-size: 17px;
}
`;

const SORTS: Array<{ code: SortKey; label: string }> = [
  { code: "name", label: "A to Z" },
  { code: "year-desc", label: "Newest first" },
  { code: "year-asc", label: "Oldest first" },
];

const SCORES = [
  { value: 0, label: "Any" },
  { value: 6, label: "6 and up" },
  { value: 7, label: "7 and up" },
  { value: 8, label: "8 and up" },
];

export class FilterSheet {
  /** The sheet, mounted by the composition root and shown by the manager. */
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly sortList: HTMLElement;
  private readonly genreHead: HTMLElement;
  private readonly genreList: HTMLElement;
  private readonly scoreHead: HTMLElement;
  private readonly scoreList: HTMLElement;
  private readonly readyHead: HTMLElement;
  private readonly readyList: HTMLElement;
  private readonly note: HTMLElement;
  private readonly clear: HTMLElement;

  constructor(
    doc: Document,
    private readonly model: LibraryModel,
    private readonly onChange: () => void,
  ) {
    this.doc = doc;
    this.sortList = h(doc, "div", { id: "sortList" });
    this.genreHead = head(doc, "Kind", "genreHead");
    this.genreList = h(doc, "div", { id: "genreList" });
    this.scoreHead = head(doc, "Score", "scoreHead");
    this.scoreList = h(doc, "div", { id: "scoreList" });
    this.readyHead = head(doc, "Headphones", "readyHead");
    this.readyList = h(doc, "div", { id: "readyList" });
    this.note = h(doc, "div", { id: "fsheetNote" });
    this.clear = h(doc, "button", {
      id: "filterClear",
      type: "button",
      text: "Show everything",
      onTap: () => {
        this.model.clearFilters();
        this.apply();
      },
    });

    this.root = h(
      doc,
      "div",
      {
        id: "fsheet",
        class: "sheet",
        role: "dialog",
        modal: true,
        aria: "Filter and sort",
      },
      [
        grip(doc),
        head(doc, "Order"),
        this.sortList,
        this.genreHead,
        this.genreList,
        this.scoreHead,
        this.scoreList,
        this.readyHead,
        this.readyList,
        this.note,
        this.clear,
      ],
    );
  }

  private apply(): void {
    this.onChange();
    this.paint();
  }

  private section(head: HTMLElement, list: HTMLElement, show: boolean): void {
    head.style.display = show ? "" : "none";
    list.style.display = show ? "" : "none";
  }

  paint(): void {
    const model = this.model;

    this.sortList.textContent = "";
    const haveYears =
      model.count((it) => !isNaN(parseInt(String(it.year), 10))) > 0;
    for (const s of SORTS) {
      if (s.code !== "name" && !haveYears) {
        continue;
      }
      this.sortList.appendChild(
        row(this.doc, s.label, "", model.sort === s.code, () => {
          model.setSort(s.code);
          this.apply();
        }),
      );
    }

    this.genreList.textContent = "";
    const genres = model.facets.genres;
    this.section(this.genreHead, this.genreList, genres.length > 0);
    if (genres.length) {
      this.genreList.appendChild(
        row(this.doc, "Everything", "", !model.genre, () => {
          model.setGenre("");
          this.apply();
        }),
      );
      for (const name of genres) {
        // The count is against the OTHER filters, so a row reads as what tapping
        // it would leave on the grid rather than as a library total.
        const n = model.count(
          (item: LibraryItem) =>
            (item.genres || []).indexOf(name) >= 0 &&
            (!(model.minScore > 0) || model.scoreOf(item) >= model.minScore) &&
            (!model.ready || (item.audio_langs || []).length > 0),
        );
        this.genreList.appendChild(
          row(this.doc, name, n === 1 ? "1 movie" : n + " movies", model.genre === name, () => {
            model.setGenre(model.genre === name ? "" : name);
            this.apply();
          }),
        );
      }
    }

    this.scoreList.textContent = "";
    const haveScores = model.count((it) => !isNaN(model.scoreOf(it))) > 0;
    this.section(this.scoreHead, this.scoreList, haveScores);
    if (haveScores) {
      for (const s of SCORES) {
        this.scoreList.appendChild(
          row(this.doc, s.label, "", model.minScore === s.value, () => {
            model.setMinScore(s.value);
            this.apply();
          }),
        );
      }
    }

    this.readyList.textContent = "";
    const haveAudio = model.count((it) => (it.audio_langs || []).length > 0) > 0;
    this.section(this.readyHead, this.readyList, haveAudio);
    if (haveAudio) {
      this.readyList.appendChild(
        row(this.doc, "All movies", "", !model.ready, () => {
          model.setReady(false);
          this.apply();
        }),
      );
      this.readyList.appendChild(
        row(this.doc, "Ready for headphones", "", model.ready, () => {
          model.setReady(true);
          this.apply();
        }),
      );
    }

    if (!genres.length && !haveScores && !haveAudio) {
      setText(
        this.note,
        "This library hasn't been prepared yet, so there is nothing to sort or filter by. Run playstick-prep.py over it.",
      );
    } else {
      setText(this.note, "");
    }
    this.clear.style.display = model.filtersOn() ? "" : "none";
  }
}
