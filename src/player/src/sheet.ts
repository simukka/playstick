// The bottom sheets: one scrim, three sheets (sound, subtitles and filter),
// never more than one open. Each opener closes the others rather than stacking,
// because a sheet is most of the screen and a second over it would leave the
// first reachable only by dismissing something that looks identical.
//
// This module owns the chrome the sheets share -- the scrim, the round openers,
// and the look of a sheet and its rows -- while each sheet builds its own
// contents.
import { h, toggleClass } from "./dom";

/* The openers sit outside every view, because .view { display: none } would
   otherwise take one away on whichever view is not showing. The sound one is
   meant to be reachable at all times, including on the grid before a film
   starts: that is not only for convenience, since tapping it there is the user
   gesture iOS wants before it will let a page play audio later. */
export const SHEET_CSS = `
#filterBtn, #audioBtn, #subsBtn {
  position: fixed; z-index: 20;
  top: calc(10px + env(safe-area-inset-top));
  width: 52px; height: 52px; border-radius: 50%;
  background: var(--card); color: var(--ink);
  display: none; align-items: center; justify-content: center;
}
/* The funnel and the CC button share a slot, and never collide: the funnel
   belongs to the grid, the CC button to a film that is playing. */
#filterBtn, #subsBtn { right: 72px; }
#audioBtn { right: 12px; }
#filterBtn.on, #audioBtn.on, #subsBtn.on { display: inline-flex; }
#filterBtn.live, #audioBtn.live, #subsBtn.live {
  background: var(--accent); color: #06121f;
}
#filterBtn svg { width: 24px; height: 24px; display: block; }
#audioBtn svg { width: 26px; height: 26px; display: block; }
#subsBtn svg { width: 26px; height: 26px; display: block; }

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

const CC_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<rect x="2.5" y="5" width="19" height="14" rx="3"/>' +
  '<path d="M10 10.2a2.6 2.6 0 1 0 0 3.6M17.5 10.2a2.6 2.6 0 1 0 0 3.6"/></svg>';

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
  /** The sheets' roots. Each sheet builds its own; this only shows them. */
  sound: HTMLElement;
  subs: HTMLElement;
  filter: HTMLElement;
  onSoundOpen: () => void;
  onSubsOpen: () => void;
  onFilterOpen: () => void;
}

type SheetName = "sound" | "subs" | "filter";

export class SheetManager {
  /** The chrome, mounted by the composition root. */
  readonly scrim: HTMLElement;
  readonly audioBtn: HTMLElement;
  readonly subsBtn: HTMLElement;
  readonly filterBtn: HTMLElement;
  // Kept as a map rather than as three fields: "open this one, close the
  // others" is one rule, and written pairwise it becomes one rule per pair --
  // which is how the third sheet would have introduced a case nobody wrote.
  private readonly roots: Record<SheetName, HTMLElement>;
  private readonly opened: Record<SheetName, () => void>;

  constructor(doc: Document, deps: SheetManagerDeps) {
    this.roots = { sound: deps.sound, subs: deps.subs, filter: deps.filter };
    this.opened = {
      sound: deps.onSoundOpen,
      subs: deps.onSubsOpen,
      filter: deps.onFilterOpen,
    };

    // Left empty: main.ts paints it with the speaker or the crossed-out speaker
    // as the audio session changes.
    this.audioBtn = h(doc, "button", {
      id: "audioBtn",
      aria: "Sound",
      onTap: () => this.show("sound", !this.isOpen("sound")),
    });
    this.subsBtn = h(doc, "button", {
      id: "subsBtn",
      aria: "Subtitles",
      html: CC_ICON,
      onTap: () => this.show("subs", !this.isOpen("subs")),
    });
    this.filterBtn = h(doc, "button", {
      id: "filterBtn",
      aria: "Filter and sort",
      html: FUNNEL_ICON,
      onTap: () => this.show("filter", !this.isOpen("filter")),
    });
    this.scrim = h(doc, "div", {
      id: "sheetScrim",
      onTap: () => this.closeAll(),
    });
  }

  isOpen(name: SheetName): boolean {
    return this.roots[name].classList.contains("on");
  }

  get soundOpen(): boolean {
    return this.isOpen("sound");
  }
  get subsOpen(): boolean {
    return this.isOpen("subs");
  }
  get filterOpen(): boolean {
    return this.isOpen("filter");
  }

  show(name: SheetName, on: boolean): void {
    for (const key of Object.keys(this.roots) as SheetName[]) {
      toggleClass(this.roots[key], "on", on && key === name);
    }
    toggleClass(
      this.scrim,
      "on",
      (Object.keys(this.roots) as SheetName[]).some((k) => this.isOpen(k)),
    );
    if (on) {
      this.opened[name]();
    }
  }

  showSound(on: boolean): void {
    this.show("sound", on);
  }
  showSubs(on: boolean): void {
    this.show("subs", on);
  }
  showFilter(on: boolean): void {
    this.show("filter", on);
  }

  closeAll(): void {
    for (const key of Object.keys(this.roots) as SheetName[]) {
      toggleClass(this.roots[key], "on", false);
    }
    toggleClass(this.scrim, "on", false);
  }
}
