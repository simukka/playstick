// The library, as data: which films exist, which the filters let through, and
// in what order. Held in the page rather than asked of the server -- /api/library
// is one small payload the page already has in full, so a filter is an array
// operation, and the alternative (query parameters) would mean this daemon
// parsing attacker-shaped strings when its whole design is opaque ids.
//
// Performance: visible() is memoised behind a dirty flag, so the common poll --
// which changes nothing about the filter -- re-sorts nothing. A change to the
// items or any filter invalidates the cache exactly once.
import type { LibraryItem } from "./types";

export type SortKey = "name" | "year-desc" | "year-asc";

export interface Facets {
  genres: string[];
  langs: string[];
}

function scoreOf(item: LibraryItem): number {
  // parseFloat, not Number: a library whose .nfo files hold a certification
  // rather than a score reads as unrated (NaN) instead of as zero, so it drops
  // out only once a threshold is actually asked for.
  return parseFloat(String(item.rating));
}

function shelfKey(item: LibraryItem): string {
  // sort_title is what prep filed the film under, so "The Fifth Element" sorts
  // at F; absent for a library nobody prepped, where the plain title is what
  // the daemon sorted on anyway.
  return String(item.sort_title || item.title || "").toLowerCase();
}

function byName(a: LibraryItem, b: LibraryItem): number {
  const ka = shelfKey(a);
  const kb = shelfKey(b);
  return ka < kb ? -1 : ka > kb ? 1 : 0;
}

export class LibraryModel {
  private items: LibraryItem[] = [];
  private _genre = "";
  private _minScore = 0;
  private _ready = false;
  private _sort: SortKey = "name";

  private cache: LibraryItem[] | null = null;
  private _facets: Facets = { genres: [], langs: [] };

  get all(): readonly LibraryItem[] {
    return this.items;
  }

  get facets(): Facets {
    return this._facets;
  }

  get genre(): string {
    return this._genre;
  }

  get minScore(): number {
    return this._minScore;
  }

  get ready(): boolean {
    return this._ready;
  }

  get sort(): SortKey {
    return this._sort;
  }

  setItems(items: LibraryItem[]): void {
    this.items = items;
    this.computeFacets();
    this.cache = null;
  }

  setGenre(genre: string): void {
    // A genre no film has cannot be selected: it would leave the grid empty
    // with a chip naming something no row can un-pick.
    if (genre && this._facets.genres.indexOf(genre) < 0) {
      genre = "";
    }
    if (genre !== this._genre) {
      this._genre = genre;
      this.cache = null;
    }
  }

  setMinScore(score: number): void {
    if (score !== this._minScore) {
      this._minScore = score;
      this.cache = null;
    }
  }

  setReady(ready: boolean): void {
    if (ready !== this._ready) {
      this._ready = ready;
      this.cache = null;
    }
  }

  setSort(sort: SortKey): void {
    if (sort !== this._sort) {
      this._sort = sort;
      this.cache = null;
    }
  }

  clearFilters(): void {
    this.setGenre("");
    this.setMinScore(0);
    this.setReady(false);
  }

  filtersOn(): boolean {
    return !!this._genre || this._minScore > 0 || this._ready;
  }

  /** The films the current filter and sort leave on the grid. Memoised. */
  visible(): readonly LibraryItem[] {
    if (this.cache !== null) {
      return this.cache;
    }
    const out: LibraryItem[] = [];
    for (const item of this.items) {
      if (this.matches(item)) {
        out.push(item);
      }
    }
    this.sortInPlace(out);
    this.cache = out;
    return out;
  }

  /** How many films pass an arbitrary predicate, for the filter-sheet counts. */
  count(predicate: (item: LibraryItem) => boolean): number {
    let n = 0;
    for (const item of this.items) {
      if (predicate(item)) {
        n++;
      }
    }
    return n;
  }

  scoreOf = scoreOf;

  private matches(item: LibraryItem): boolean {
    // The admin/curator view was cut, so a hidden film simply leaves the grid;
    // there is no listener that keeps it.
    if (item.hidden) {
      return false;
    }
    if (this._genre && (item.genres || []).indexOf(this._genre) < 0) {
      return false;
    }
    if (this._minScore > 0 && !(scoreOf(item) >= this._minScore)) {
      return false;
    }
    if (this._ready && !(item.audio_langs || []).length) {
      return false;
    }
    return true;
  }

  private sortInPlace(items: LibraryItem[]): void {
    if (this._sort === "name") {
      // The server's order IS this order -- prep files by a normalised title
      // and the daemon keeps it verbatim -- so re-sorting would only introduce
      // a way for the two to disagree.
      return;
    }
    const dir = this._sort === "year-asc" ? 1 : -1;
    items.sort((a, b) => {
      const ya = parseInt(String(a.year), 10);
      const yb = parseInt(String(b.year), 10);
      const na = isNaN(ya);
      const nb = isNaN(yb);
      // A film with no year sits at the end of BOTH year sorts: it is not the
      // oldest and not the newest, it is unknown, and burying it under "oldest
      // first" would be a lie the grid tells silently.
      if (na || nb) {
        return na && nb ? byName(a, b) : na ? 1 : -1;
      }
      return ya === yb ? byName(a, b) : (ya - yb) * dir;
    });
  }

  private computeFacets(): void {
    const genres = new Set<string>();
    const langs = new Set<string>();
    for (const item of this.items) {
      if (item.genres) {
        for (const g of item.genres) {
          genres.add(g);
        }
      }
      if (item.audio_langs) {
        for (const code of item.audio_langs) {
          langs.add(code);
        }
      }
    }
    this._facets = {
      genres: [...genres].sort(),
      langs: [...langs].sort(),
    };
    // A filter must not outlive the films it named.
    if (this._genre && this._facets.genres.indexOf(this._genre) < 0) {
      this._genre = "";
    }
  }
}
