// The bottom sheets: one scrim, two sheets (sound and filter), never more than
// one open. Each opener closes the other rather than stacking, because a sheet
// is most of the screen and a second over it would leave the first reachable
// only by dismissing something that looks identical.
//
// This module owns the chrome both sheets share -- the scrim, the two round
// openers, and the look of a sheet and its rows -- while each sheet builds its
// own contents.
import { h, toggleClass } from "./dom";

/* The openers sit outside every view, because .view { display: none } would
   otherwise take one away on whichever view is not showing. The sound one is
   meant to be reachable at all times, including on the grid before a film
   starts: that is not only for convenience, since tapping it there is the user
   gesture iOS wants before it will let a page play audio later. */
export const SHEET_CSS = `
#filterBtn, #audioBtn {
  position: fixed; z-index: 20;
  top: calc(10px + env(safe-area-inset-top));
  width: 52px; height: 52px; border-radius: 50%;
  background: var(--card); color: var(--ink);
  display: none; align-items: center; justify-content: center;
}
#filterBtn { right: 72px; }
#audioBtn { right: 12px; }
#filterBtn.on, #audioBtn.on { display: inline-flex; }
#filterBtn.live, #audioBtn.live { background: var(--accent); color: #06121f; }
#filterBtn svg { width: 24px; height: 24px; display: block; }
#audioBtn svg { width: 26px; height: 26px; display: block; }

#sheetScrim {
  position: fixed; inset: 0; z-index: 30; background: rgba(0,0,0,.6);
  display: none;
}
#sheetScrim.on { display: block; }

/* Two sheets -- Sound and Filters -- sharing one scrim and one set of rules.
   Only ever one of them open at a time. */
.sheet {
  position: fixed; z-index: 31; left: 0; right: 0; bottom: 0;
  background: #1b1b21; border-radius: 22px 22px 0 0;
  padding: 8px 12px calc(18px + env(safe-area-inset-bottom));
  max-height: 82vh; overflow-y: auto;
  display: none;
}
.sheet.on { display: block; }
.grip {
  width: 44px; height: 5px; border-radius: 3px; background: #3a3a45;
  margin: 8px auto 4px;
}
.sheet h2 {
  font-size: 14px; font-weight: 700; letter-spacing: .8px;
  text-transform: uppercase; color: var(--dim); margin: 18px 6px 8px;
}
/* Rows are big for the same reason the poster tiles are: a child in a dark
   room is the person using this. */
.row {
  display: flex; align-items: center; gap: 14px; width: 100%;
  min-height: 62px; padding: 0 14px; border-radius: 14px;
  font-size: 19px; text-align: left;
}
.row:active { background: #26262e; }
.row.sel { background: #26262e; }
.row .tick { margin-left: auto; color: var(--accent); font-size: 22px; }
.row .sub { display: block; font-size: 14px; color: var(--dim); margin-top: 2px; }
.row .grow { flex: 1; min-width: 0; }
`;

const FUNNEL_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>';

/** A tappable row with an optional sub-label and a tick when selected. Shared by
 * both sheets so the two read and behave identically. */
export function row(
  doc: Document,
  label: string,
  sub: string,
  selected: boolean,
  onTap: () => void,
): HTMLElement {
  const text = h(doc, "span", { class: "grow", text: label });
  if (sub) {
    text.appendChild(h(doc, "span", { class: "sub", text: sub }));
  }
  const kids = [text];
  if (selected) {
    kids.push(h(doc, "span", { class: "tick", html: "✓" })); // ✓
  }
  return h(
    doc,
    "button",
    { class: selected ? "row sel" : "row", type: "button", onTap },
    kids,
  );
}

const LANG_NAMES: Record<string, string> = {
  eng: "English", en: "English", fin: "Suomi", fi: "Suomi",
  swe: "Svenska", sv: "Svenska", nor: "Norsk", no: "Norsk",
  dan: "Dansk", da: "Dansk", deu: "Deutsch", ger: "Deutsch", de: "Deutsch",
  fra: "Français", fre: "Français", fr: "Français",
  spa: "Español", es: "Español", ita: "Italiano", it: "Italiano",
  nld: "Nederlands", dut: "Nederlands", nl: "Nederlands",
  pol: "Polski", pl: "Polski", rus: "Русский", ru: "Русский",
  jpn: "日本語", ja: "日本語", est: "Eesti", et: "Eesti",
};

export function langName(code: string, title?: string): string {
  if (title) {
    return title;
  }
  return LANG_NAMES[code] || (code && code !== "und" ? code.toUpperCase() : "Sound");
}

export interface SheetManagerDeps {
  /** The two sheets' roots. Each sheet builds its own; this only shows them. */
  sound: HTMLElement;
  filter: HTMLElement;
  onSoundOpen: () => void;
  onFilterOpen: () => void;
}

export class SheetManager {
  /** The chrome, mounted by the composition root. */
  readonly scrim: HTMLElement;
  readonly audioBtn: HTMLElement;
  readonly filterBtn: HTMLElement;
  private readonly sheet: HTMLElement;
  private readonly fsheet: HTMLElement;

  constructor(doc: Document, private readonly deps: SheetManagerDeps) {
    this.sheet = deps.sound;
    this.fsheet = deps.filter;

    // Left empty: main.ts paints it with the speaker or the crossed-out speaker
    // as the audio session changes.
    this.audioBtn = h(doc, "button", {
      id: "audioBtn",
      aria: "Sound",
      onTap: () => this.showSound(!this.soundOpen),
    });
    this.filterBtn = h(doc, "button", {
      id: "filterBtn",
      aria: "Filter and sort",
      html: FUNNEL_ICON,
      onTap: () => this.showFilter(!this.filterOpen),
    });
    this.scrim = h(doc, "div", {
      id: "sheetScrim",
      onTap: () => {
        this.showSound(false);
        this.showFilter(false);
      },
    });
  }

  get soundOpen(): boolean {
    return this.sheet.classList.contains("on");
  }
  get filterOpen(): boolean {
    return this.fsheet.classList.contains("on");
  }

  showSound(on: boolean): void {
    toggleClass(this.sheet, "on", on);
    if (on) {
      toggleClass(this.fsheet, "on", false);
    }
    this.paintScrim();
    if (on) {
      this.deps.onSoundOpen();
    }
  }

  showFilter(on: boolean): void {
    toggleClass(this.fsheet, "on", on);
    if (on) {
      toggleClass(this.sheet, "on", false);
    }
    this.paintScrim();
    if (on) {
      this.deps.onFilterOpen();
    }
  }

  private paintScrim(): void {
    toggleClass(this.scrim, "on", this.soundOpen || this.filterOpen);
  }
}
