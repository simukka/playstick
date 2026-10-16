// The library, as data: which films exist, which the filters let through, and
// in what order. Held in the page rather than asked of the server -- /api/library
// is one small payload the page already has in full, so a filter is an array
// operation, and the alternative (query parameters) would mean this daemon
// parsing attacker-shaped strings when its whole design is opaque ids.
//
// Performance: visible() is memoised behind a dirty flag, so the common poll --
// which changes nothing about the filter -- re-sorts nothing. A change to the
// items or any filter invalidates the cache exactly once.
//
// EPISODES ARE FOLDED HERE. The daemon sends every episode as a row of its own,
// because each one is a file with its own id, poster, soundtracks and resume
// position. The grid shows a series as ONE tile: twenty-four near-identical
// posters are a wall a child has to scroll past to reach the next film. The tile
// stands where the series' first episode would, and tapping it opens the
// episode sheet rather than playing anything.
import type { LibraryItem } from "./types";

export type SortKey = "name" | "year-desc" | "year-asc";

/** What the grid draws: a film, or a series standing in for its episodes. */
export type GridItem = LibraryItem & { thumb?: string };

/** The prefix a series tile's id carries. A daemon id is sixteen hex
 * characters, so the two cannot collide -- and a tap on a series is therefore
 * never mistaken for a film to send to /api/play. */
export const SHOW_PREFIX = "show:";

export interface Show {
  /** The tile's id: SHOW_PREFIX and the series' key. */
  id: string;
  title: string;
  /** Every episode the children may see, by season and then episode, with
   * specials after the last season. */
  episodes: LibraryItem[];
  /** The seasons those episodes fall in, in the same order. */
  seasons: number[];
  /** The tile itself, built once per library so the grid's reconciler sees
   * the same object poll after poll. */
  tile: GridItem;
}

export function isEpisode(item: LibraryItem): boolean {
  return !!item.series && item.season != null && item.episode != null;
}

/** "Bluey S1 E2 · Hospital": what the playing screen calls an episode, the
 * same words the daemon's full_title uses. A film is just its title. */
export function fullTitle(item: LibraryItem): string {
  if (!isEpisode(item)) {
    return item.title;
  }
  const label = `${item.series} S${item.season} E${item.episode}`;
  return item.title ? `${label} · ${item.title}` : label;
}

function showKey(item: LibraryItem): string {
  // Case and spacing only. Two spellings that differ by more than that are two
  // shows as far as the page can tell, and guessing otherwise is how one show's
  // episodes would end up inside another's tile.
  return String(item.series).trim().replace(/\s+/g, " ").toLowerCase();
}

/** Specials are season 0 but belong after the seasons, not before them: a child
 * opening a show should land on its first episode. */
function seasonRank(season: number): number {
  return season === 0 ? Number.MAX_SAFE_INTEGER : season;
}

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

  private cache: GridItem[] | null = null;
  private _facets: Facets = { genres: [], langs: [] };
  private shows = new Map<string, Show>();

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
    this.computeShows();
    this.cache = null;
  }

  /** The series behind a tile id, or undefined when the id is a film's. */
  show(id: string): Show | undefined {
    if (id.indexOf(SHOW_PREFIX) !== 0) {
      return undefined;
    }
    return this.shows.get(id.slice(SHOW_PREFIX.length));
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

  /** The tiles the current filter and sort leave on the grid: films, and one
   * per series any of whose episodes the filter lets through. Memoised. */
  visible(): readonly GridItem[] {
    if (this.cache !== null) {
      return this.cache;
    }
    const out: GridItem[] = [];
    const folded = new Set<string>();
    for (const item of this.items) {
      if (!this.matches(item)) {
        continue;
      }
      if (isEpisode(item)) {
        const key = showKey(item);
        const show = this.shows.get(key);
        if (show && !folded.has(key)) {
          folded.add(key);
          out.push(show.tile);
        }
        continue;
      }
      out.push(item);
    }
    this.sortInPlace(out);
    this.cache = out;
    return out;
  }

  /** How many tiles pass an arbitrary predicate, for the filter-sheet counts. A
   * series counts once however many of its episodes pass: the count says what
   * tapping a row would leave on the grid, and that is one poster. */
  count(predicate: (item: LibraryItem) => boolean): number {
    let n = 0;
    const seen = new Set<string>();
    for (const item of this.items) {
      if (!predicate(item)) {
        continue;
      }
      if (isEpisode(item)) {
        const key = showKey(item);
        if (seen.has(key)) {
          continue;
        }
        seen.add(key);
      }
      n++;
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

  private sortInPlace(items: GridItem[]): void {
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

  private computeShows(): void {
    const shows = new Map<string, Show>();
    for (const item of this.items) {
      // A hidden episode is out of the picker as well as off the grid -- the
      // same rule a hidden film gets.
      if (item.hidden || !isEpisode(item)) {
        continue;
      }
      const key = showKey(item);
      let show = shows.get(key);
      if (!show) {
        show = {
          id: SHOW_PREFIX + key,
          title: String(item.series).trim(),
          episodes: [],
          seasons: [],
          tile: { id: SHOW_PREFIX + key, title: "" },
        };
        shows.set(key, show);
      }
      show.episodes.push(item);
    }
    for (const show of shows.values()) {
      show.episodes.sort(
        (a, b) =>
          seasonRank(a.season!) - seasonRank(b.season!) || a.episode! - b.episode!,
      );
      for (const ep of show.episodes) {
        if (show.seasons.indexOf(ep.season!) < 0) {
          show.seasons.push(ep.season!);
        }
      }
      // The first episode stands for the show: its poster, and its shelf key,
      // which the preparer derived from the series' name so the tile files
      // where the show would.
      const first = show.episodes[0]!;
      show.tile = {
        id: show.id,
        title: show.title,
        sort_title: first.sort_title,
        year: first.year,
        rating: first.rating,
        genres: first.genres,
        has_thumb: first.has_thumb,
        thumb: first.id,
        series: show.title,
      };
    }
    this.shows = shows;
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
