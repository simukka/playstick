// The grid view: the reconciled tiles, the empty-state message, and the filter
// chip. It builds its own markup, owns a LibraryModel and repaints from it.
// render() is called when the library or a filter changes, not on every poll --
// the model's memoised visible() means an unchanged render is cheap, and the
// grid reconciler makes it cheaper still by moving nothing.
import { h, setText, toggleClass, Grid } from "./dom";
import type { LibraryModel } from "./library";

export const LIBRARY_CSS = `
h1 { font-size: 22px; margin: 18px 4px 12px; font-weight: 650; letter-spacing: .2px; }
#grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
}
.tile {
  display: block; width: 100%; text-align: left;
  border-radius: 16px; overflow: hidden; background: var(--card);
  transition: transform .08s ease;
}
.tile:active { transform: scale(.96); }
.tile img {
  display: block; width: 100%; aspect-ratio: 2 / 3; object-fit: cover;
  background: #202028;
}
.tile span {
  display: block; padding: 10px 10px 12px; font-size: 16px; line-height: 1.25;
  font-weight: 600;
  /* Two lines, then ellipsis: a long filename must not push the grid around. */
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
#grid.blocked { opacity: .35; pointer-events: none; }
#empty { color: var(--dim); font-size: 18px; padding: 40px 8px; line-height: 1.5; }
/* The grid is empty because of a filter, not because the library is. Big,
   because it is the whole way back: a child who has hidden every film must not
   have to find the funnel again to undo it. */
#emptyClear {
  display: none; margin: 8px auto 0; min-height: 68px; width: 100%;
  max-width: 420px; border-radius: 18px; background: var(--card);
  font-size: 19px; font-weight: 650;
}
#emptyClear.on { display: block; }

#filterChip {
  display: none; align-items: center; gap: 10px;
  margin: 0 4px 12px; padding: 12px 16px; border-radius: 14px;
  background: var(--card); font-size: 17px; width: 100%; text-align: left;
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

  constructor(
    doc: Document,
    private readonly model: LibraryModel,
    stamp: (path: string) => string,
    onPlay: (id: string) => void,
    /** The funnel, built by the sheet manager: lit while a filter is on. */
    private readonly filterBtn: HTMLElement,
    now: () => number = Date.now,
  ) {
    this.doc = doc;
    this.gridEl = h(doc, "div", { id: "grid" });
    this.grid = new Grid(doc, this.gridEl, stamp, onPlay, now);
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
    this.root = h(doc, "div", { id: "library", class: "view on" }, [
      h(doc, "h1", { text: "Playstick" }),
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
    this.paintChip();
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
    const on = this.model.filtersOn();
    toggleClass(this.chip, "on", on);
    toggleClass(this.filterBtn, "live", on);
    const text = on ? this.describe() : "";
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
    if (this.model.genre) {
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
