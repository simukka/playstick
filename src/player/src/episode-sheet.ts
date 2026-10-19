// The episode sheet: what tapping a series' poster opens. The show's name, a
// row of season buttons when there is more than one season, and that season's
// episodes as rows -- tap one and it plays.
//
// A sheet rather than a view of its own, because it is a choice made on the way
// to playing something, exactly like the filter: it sits over the grid it came
// from, and the scrim is the way back.
import { h, grip, setText } from "./dom";
import { row } from "./sheet";
import type { Show } from "./library";
import type { LibraryItem } from "./types";

export const EPISODE_CSS = `
#esheet h2#esheetTitle {
  font-size: 22px; font-weight: 650; letter-spacing: .2px; text-transform: none;
  color: var(--ink); margin: 14px 6px 10px;
}
/* Big, for the same reason the rows are -- and a row that scrolls sideways
   rather than wraps, so a show with nine seasons does not push the episodes
   off the bottom of the sheet. */
#esheetSeasons {
  display: flex; gap: 8px; overflow-x: auto; padding: 2px 6px 10px;
  -webkit-overflow-scrolling: touch;
}
#esheetSeasons button {
  flex: none; min-height: 44px; padding: 0 18px; border-radius: 12px;
  background: var(--card); border: 1px solid var(--line);
  font-size: 16px; font-weight: 600;
}
#esheetSeasons button.sel { background: var(--accent); border-color: var(--accent); color: #fff; }
/* An episode row plays it: an action, not a choice, so no box to tick. */
#esheet .row::after { display: none; }
`;

export function seasonName(season: number): string {
  return season === 0 ? "Specials" : "Season " + season;
}

/** What a row says. The number first, because that is what a child counting
 * along a series is looking for; the title after it, unless the title is only
 * the number again, which the admin app writes when the file had no name. */
export function episodeName(ep: LibraryItem): string {
  const title = (ep.title || "").trim();
  if (!title || /^episode \d+$/i.test(title)) {
    return "Episode " + ep.episode;
  }
  return ep.episode + ". " + title;
}

export class EpisodeSheet {
  /** The sheet, mounted by the composition root and shown by the manager. */
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly title: HTMLElement;
  private readonly seasons: HTMLElement;
  private readonly list: HTMLElement;
  private show: Show | null = null;
  private season = 0;

  constructor(
    doc: Document,
    private readonly onPick: (id: string) => void,
  ) {
    this.doc = doc;
    this.title = h(doc, "h2", { id: "esheetTitle" });
    this.seasons = h(doc, "div", { id: "esheetSeasons", role: "tablist" });
    this.list = h(doc, "div", { id: "esheetList" });
    this.root = h(
      doc,
      "div",
      { id: "esheet", class: "sheet", role: "dialog", modal: true, aria: "Episodes" },
      [grip(doc), this.title, this.seasons, this.list],
    );
  }

  /** Point the sheet at a show. Called before the manager opens it; the
   * manager's open hook then paints. Opens on the first season every time --
   * where somebody left off is the daemon's to know, not this sheet's. */
  load(show: Show): void {
    this.show = show;
    this.season = show.seasons[0] ?? 0;
  }

  /** The show on the sheet, for the composition root to refresh after a new
   * library arrives. */
  get current(): Show | null {
    return this.show;
  }

  paint(): void {
    const show = this.show;
    this.seasons.textContent = "";
    this.list.textContent = "";
    if (!show) {
      setText(this.title, "");
      return;
    }
    setText(this.title, show.title);
    // A new library may have taken the season away; fall back rather than
    // draw an empty list under a button that no longer exists.
    if (show.seasons.indexOf(this.season) < 0) {
      this.season = show.seasons[0] ?? 0;
    }

    // One season has nothing to choose between, and a lone button reads as a
    // control that does nothing.
    this.seasons.hidden = show.seasons.length < 2;
    for (const season of show.seasons) {
      const on = season === this.season;
      const btn = h(this.doc, "button", {
        type: "button",
        role: "tab",
        class: on ? "sel" : "",
        text: seasonName(season),
        onTap: () => {
          this.season = season;
          this.paint();
        },
      });
      btn.setAttribute("aria-selected", on ? "true" : "false");
      this.seasons.appendChild(btn);
    }

    for (const ep of show.episodes) {
      if (ep.season !== this.season) {
        continue;
      }
      const id = ep.id;
      this.list.appendChild(row(this.doc, episodeName(ep), "", false, () => this.onPick(id)));
    }
  }
}
