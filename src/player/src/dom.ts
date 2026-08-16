// The thin, typed seam over the document: the element builder every view builds
// its markup with, plus the one piece of the view that is worth engineering, the
// grid reconciler.
//
// The grid is the hot render. A poll arrives, a poster finishes extracting, a
// filter changes the visible set -- and the shipped page answered every one of
// those by clearing the grid and rebuilding every tile from scratch, discarding
// and re-decoding posters a phone had already painted. This keeps a tile per
// film keyed by id, reuses it across updates, and moves nodes only when the
// order actually changed. An unchanged poll touches the DOM zero times.

/** The attributes the page actually sets. Deliberately not a general-purpose
 * mirror of HTMLElement: a builder that can express anything is one nobody can
 * read, and everything the UI needs is here. */
export interface Attrs {
  id?: string;
  class?: string;
  /** Text content. Prefer this to `html` -- it cannot be a markup mistake. */
  text?: string;
  /** Raw markup, for the few glyphs and inline SVG icons that need it. */
  html?: string;
  /** A button's type, always "button": a bare <button> in a page with no form
   * is harmless, but saying so is one less thing to reason about. */
  type?: "button";
  /** aria-label. */
  aria?: string;
  role?: string;
  /** aria-modal="true", for the sheets. */
  modal?: boolean;
  hidden?: boolean;
  alt?: string;
  onTap?: () => void;
}

/**
 * Build an element. Everything goes through the injected `doc`, so the views
 * construct their markup against the test stub exactly as they do against a
 * browser -- same seam the grid reconciler and the sheet rows already use.
 */
export function h(
  doc: Document,
  tag: string,
  a: Attrs = {},
  kids: readonly HTMLElement[] = [],
): HTMLElement {
  const el = doc.createElement(tag);
  if (a.id !== undefined) {
    el.id = a.id;
  }
  if (a.class !== undefined) {
    el.className = a.class;
  }
  if (a.text !== undefined) {
    el.textContent = a.text;
  }
  if (a.html !== undefined) {
    el.innerHTML = a.html;
  }
  if (a.type !== undefined) {
    (el as HTMLButtonElement).type = a.type;
  }
  if (a.aria !== undefined) {
    el.setAttribute("aria-label", a.aria);
  }
  if (a.role !== undefined) {
    el.setAttribute("role", a.role);
  }
  if (a.modal) {
    el.setAttribute("aria-modal", "true");
  }
  if (a.hidden !== undefined) {
    el.hidden = a.hidden;
  }
  if (a.alt !== undefined) {
    (el as HTMLImageElement).alt = a.alt;
  }
  if (a.onTap) {
    el.addEventListener("click", a.onTap);
  }
  for (const kid of kids) {
    el.appendChild(kid);
  }
  return el;
}

/** A sheet's drag handle: no behaviour, just the thing that says "this pulls
 * down". Both sheets open with one. */
export function grip(doc: Document): HTMLElement {
  return h(doc, "div", { class: "grip" });
}

/** A sheet section heading. */
export function head(doc: Document, text: string, id?: string): HTMLElement {
  return h(doc, "h2", { id, text });
}

/** Write text only when it changed: a no-op assignment still costs a layout
 * invalidation in some engines, and most poll updates change nothing. */
export function setText(el: HTMLElement, text: string): void {
  if (el.textContent !== text) {
    el.textContent = text;
  }
}

export function toggleClass(el: HTMLElement, cls: string, on: boolean): void {
  el.classList.toggle(cls, on);
}

/** The minimum a tile needs from a film. */
export interface TileItem {
  id: string;
  title: string;
  has_thumb?: boolean;
}

interface Tile {
  root: HTMLElement;
  img: HTMLImageElement;
  label: HTMLElement;
  title: string;
  hasThumb: boolean;
}

export class Grid {
  private tiles = new Map<string, Tile>();

  constructor(
    private readonly doc: Document,
    private readonly parent: HTMLElement,
    private readonly stamp: (path: string) => string,
    private readonly onPlay: (id: string) => void,
    private readonly now: () => number = Date.now,
  ) {}

  /** Bring the grid into line with `items`, keyed by id, minimal moves. */
  update(items: readonly TileItem[]): void {
    const parent = this.parent;

    // Drop tiles for films that left the visible set.
    if (this.tiles.size > items.length) {
      const wanted = new Set<string>();
      for (const it of items) {
        wanted.add(it.id);
      }
      for (const [id, tile] of this.tiles) {
        if (!wanted.has(id)) {
          tile.root.remove();
          this.tiles.delete(id);
        }
      }
    }

    for (let idx = 0; idx < items.length; idx++) {
      const it = items[idx]!;
      let tile = this.tiles.get(it.id);
      if (!tile) {
        tile = this.make(it);
        this.tiles.set(it.id, tile);
      } else if (tile.title !== it.title) {
        // A rename (the editor is gone, but a re-prep can still change a title).
        tile.label.textContent = it.title;
        tile.title = it.title;
      }

      // A poster that has just finished extracting. The placeholder is served
      // no-store, so a fresh request gets the real frame; the &t is which second
      // this tile gave up on the placeholder.
      if (it.has_thumb && !tile.hasThumb) {
        tile.img.src = this.stamp("/api/thumb/" + it.id) + "&t=" + this.now();
      }
      tile.hasThumb = !!it.has_thumb;

      // Move into position only if something is actually out of order.
      const current = parent.childNodes[idx];
      if (current !== tile.root) {
        parent.insertBefore(tile.root, current ?? null);
      }
    }
  }

  /** Number of live tile nodes, for the churn tests. */
  get size(): number {
    return this.tiles.size;
  }

  private make(it: TileItem): Tile {
    const root = this.doc.createElement("button");
    root.className = "tile";
    (root as HTMLButtonElement).type = "button";

    const img = this.doc.createElement("img") as HTMLImageElement;
    img.src = this.stamp("/api/thumb/" + it.id);
    img.alt = "";
    img.loading = "lazy";

    const label = this.doc.createElement("span");
    label.textContent = it.title;

    root.appendChild(img);
    root.appendChild(label);
    const id = it.id;
    root.addEventListener("click", () => this.onPlay(id));

    return { root, img, label, title: it.title, hasThumb: !!it.has_thumb };
  }
}
