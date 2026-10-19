// The grid view: the reconciled tiles, the empty-state message, and the filter
// chip. It builds its own markup, owns a LibraryModel and repaints from it.
// render() is called when the library or a filter changes, not on every poll --
// the model's memoised visible() means an unchanged render is cheap, and the
// grid reconciler makes it cheaper still by moving nothing.
import { h, setText, toggleClass, Grid } from "./dom";
import type { LibraryModel } from "./library";
import { WORDMARK } from "./brand";

export const LIBRARY_CSS = `
h1 { font-size: 22px; margin: 18px 4px 6px; font-weight: 650; letter-spacing: .2px; }
/* The wordmark, lined up with the round buttons beside it rather than with
   the text it replaced. */
h1 svg { display: block; height: 28px; width: auto; margin-top: 6px; }
#libHead { margin: 18px 4px 14px; }
#libHead h2 { font-size: 30px; font-weight: 750; margin: 0; letter-spacing: .1px; }
#libCount { color: var(--dim); font-size: 16px; margin-top: 2px; }

/* The genres, one tap each, in a row that scrolls sideways rather than wraps:
   the same filter as the sheet's Kind list, without opening the sheet. */
#chips {
  display: flex; gap: 8px; overflow-x: auto; margin: 0 -12px 16px;
  padding: 2px 16px 4px; scrollbar-width: none; -webkit-overflow-scrolling: touch;
}
#chips::-webkit-scrollbar { display: none; }
#chips button {
  flex: none; min-height: 44px; padding: 0 18px; border-radius: 12px;
  background: var(--card); border: 1px solid var(--line);
  font-size: 15px; font-weight: 550; color: #c9d2e6;
}
#chips button.sel { background: var(--accent); border-color: var(--accent); color: #fff; }

#grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
}
/* The poster is the tile: the title and year sit on it, over a fade to black
   at the bottom so they read on any artwork. */
.tile {
  position: relative; display: block; width: 100%; text-align: left;
  border-radius: 18px; overflow: hidden; background: var(--card);
  border: 1px solid var(--line);
  transition: transform .08s ease;
}
.tile:active { transform: scale(.96); }
.tile img {
  display: block; width: 100%; aspect-ratio: 2 / 3; object-fit: cover;
  background: var(--card2);
}
.tile::before {
  content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 55%;
  background: linear-gradient(to bottom, rgba(7,8,15,0), rgba(7,8,15,.92));
  pointer-events: none;
}
.tile span {
  position: absolute; left: 12px; right: 12px; bottom: 32px;
  font-size: 17px; line-height: 1.2; font-weight: 700;
  text-shadow: 0 1px 6px rgba(0,0,0,.6);
  /* Two lines, then ellipsis: a long filename must not push the grid around. */
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.tile::after {
  content: attr(data-year); position: absolute; left: 12px; bottom: 11px;
  font-size: 14px; color: #c9d2e6;
}
.tile:not([data-year]) span { bottom: 12px; }
#grid.blocked { opacity: .35; pointer-events: none; }
#empty { color: var(--dim); font-size: 18px; padding: 40px 8px; line-height: 1.5; }
/* The grid is empty because of a filter, not because the library is. Big,
   because it is the whole way back: a child who has hidden every film must not
   have to find the funnel again to undo it. */
#emptyClear {
  display: none; margin: 8px auto 0; min-height: 68px; width: 100%;
  max-width: 420px; border-radius: 18px; background: var(--grad); color: #fff;
  font-size: 19px; font-weight: 650;
}
#emptyClear.on { display: block; }

#filterChip {
  display: none; align-items: center; gap: 10px;
  margin: 0 4px 14px; padding: 12px 16px; border-radius: 14px;
  background: var(--card); border: 1px solid var(--line);
  font-size: 17px; width: 100%; text-align: left;
}
#filterChip.on { display: flex; }
#filterChip .grow { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
#filterChip .x { color: var(--dim); font-size: 20px; }
`;

export class LibraryView {
  /** The view, mounted by the composition root and shown by the switcher. */
  readonly root: HTMLElement;
  /** The tiles' parent, which the presenter blocks during AirPlay. */
  readonly gridEl: HTMLElement;
  private readonly doc: Document;
  private readonly grid: Grid;
  private readonly empty: HTMLElement;
  private readonly emptyClear: HTMLElement;
  private readonly chip: HTMLElement;
  private chipText = "";
  private readonly count: HTMLElement;
  private readonly chips: HTMLElement;
  private chipsKey = "";

  constructor(
    doc: Document,
    private readonly model: LibraryModel,
    stamp: (path: string) => string,
    /** A tile was tapped: a film's id, or a series' (see LibraryModel.show). */
    onOpen: (id: string) => void,
    /** The funnel, built by the sheet manager: lit while a filter is on. */
    private readonly filterBtn: HTMLElement,
    now: () => number = Date.now,
  ) {
    this.doc = doc;
    this.gridEl = h(doc, "div", { id: "grid" });
    this.grid = new Grid(doc, this.gridEl, stamp, onOpen, now);
    this.empty = h(doc, "div", { id: "empty", hidden: true });
    // Both ways out of an over-filtered grid do the same thing; this is the one
    // that is already under the child's thumb when they find themselves looking
    // at nothing.
    this.emptyClear = h(doc, "button", {
      id: "emptyClear",
      type: "button",
      text: "Show everything",
      onTap: () => this.clearFilters(),
    });
    this.chip = h(doc, "button", {
      id: "filterChip",
      type: "button",
      onTap: () => this.clearFilters(),
    });
    this.count = h(doc, "div", { id: "libCount" });
    this.chips = h(doc, "div", { id: "chips" });
    this.root = h(doc, "div", { id: "library", class: "view on" }, [
      h(doc, "h1", { html: WORDMARK }),
      h(doc, "div", { id: "libHead" }, [h(doc, "h2", { text: "Library" }), this.count]),
      this.chips,
      this.chip,
      this.gridEl,
      this.empty,
      this.emptyClear,
    ]);
  }

  render(): void {
    const items = this.model.visible();
    this.grid.update(items);

    const emptyByFilter = this.model.filtersOn() && this.model.all.length > 0;
    this.empty.hidden = items.length > 0;
    if (!items.length) {
      setText(
        this.empty,
        emptyByFilter ? "Nothing matches what you picked." : "No movies found yet.",
      );
    }
    // Only ever offered when clearing would actually bring films back.
    toggleClass(this.emptyClear, "on", items.length === 0 && emptyByFilter);
    setText(this.count, items.length === 1 ? "1 film" : items.length + " films");
    this.paintChip();
    this.paintChips();
  }

  /** "All" and each genre, the most-stocked first. Rebuilt only when the
   * genres or the choice change, like the filter chip below it. */
  private paintChips(): void {
    const genres = this.model.facets.genres;
    const key = genres.join("\n") + "|" + this.model.genre;
    if (key === this.chipsKey) {
      return;
    }
    this.chipsKey = key;
    this.chips.textContent = "";
    this.chips.hidden = genres.length === 0;
    if (!genres.length) {
      return;
    }
    const counts = new Map<string, number>();
    for (const g of genres) {
      counts.set(g, this.model.count((item) => (item.genres || []).indexOf(g) >= 0));
    }
    const ordered = [...genres].sort(
      (a, b) => counts.get(b)! - counts.get(a)! || a.localeCompare(b),
    );
    const chip = (label: string, genre: string): HTMLElement =>
      h(this.doc, "button", {
        type: "button",
        class: this.model.genre === genre ? "sel" : "",
        text: label,
        onTap: () => {
          this.model.setGenre(genre);
          this.render();
        },
      });
    this.chips.appendChild(chip("All", ""));
    for (const g of ordered) {
      this.chips.appendChild(chip(g, g));
    }
  }

  /** The daemon could not reach the NAS: say so where the grid would be. */
  unavailable(): void {
    this.empty.hidden = false;
    setText(this.empty, "Can't reach the movies right now.");
    toggleClass(this.emptyClear, "on", false);
  }

  private clearFilters(): void {
    this.model.clearFilters();
    this.render();
  }

  private paintChip(): void {
    const filtered = this.model.filtersOn();
    toggleClass(this.filterBtn, "live", filtered);
    const text = filtered ? this.describe() : "";
    // Only for what the genre chips above do not already show.
    const on = text !== "";
    toggleClass(this.chip, "on", on);
    if (text === this.chipText) {
      return; // no churn when the filter is unchanged
    }
    this.chipText = text;
    this.chip.textContent = "";
    if (!on) {
      return;
    }
    // Two children so the stylesheet can lay out the label and the clear
    // affordance; the whole chip is the tap target that clears the filter.
    this.chip.appendChild(h(this.doc, "span", { class: "grow", text }));
    this.chip.appendChild(h(this.doc, "span", { class: "x", html: "✕" })); // ✕
  }

  private describe(): string {
    const parts: string[] = [];
    // The chip row says which genre is chosen, when there is one to say it.
    if (this.model.genre && !this.model.facets.genres.length) {
      parts.push(this.model.genre);
    }
    if (this.model.minScore > 0) {
      parts.push(this.model.minScore + "+");
    }
    if (this.model.ready) {
      parts.push("headphones");
    }
    return parts.join(" · ");
  }
}
