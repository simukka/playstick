// The three views and the banner. Each one builds its own markup and keeps the
// handful of nodes it writes to, so there is no id lookup and no second file to
// keep in step; nothing here decides WHEN to show what -- that is the
// presenter's job -- so each of these is a set of small, order-free writes that
// a test can drive against the DOM stub and read straight back.
import { h, setText, toggleClass } from "./dom";
import { timeLeft, barWidth, clockTime, jumpLabel } from "./format";
import { TAIL } from "./seek";
import type { Status } from "./types";

export type ViewName = "library" | "preparing" | "playing";

const PLAY_GLYPH = "\u25B6"; // ▶
const PAUSE_GLYPH = "\u2759\u2759"; // ❙❙

/* The skip icons: a circular arrow with the number of seconds inside it, which
   is the one transport idiom a child has already met on somebody's tablet. Drawn
   rather than set in text because the "10" has to sit inside the arc. */
const skipIcon = (back: boolean): string =>
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  (back
    ? '<path d="M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6"/>'
    : '<path d="M12 5V1l5 5-5 5V7a6 6 0 1 0 6 6"/>') +
  '<text x="12" y="16.6" text-anchor="middle" font-size="8" font-weight="700" ' +
  'stroke="none" fill="currentColor" font-family="system-ui, sans-serif">10</text>' +
  "</svg>";

export const BANNER_CSS = `
#banner {
  display: none; margin: 12px 0 4px; padding: 14px 16px; border-radius: 14px;
  background: #2a2317; color: #ffd79a; font-size: 17px; line-height: 1.35;
}
#banner.on { display: block; }
/* Sized like the other things a child taps rather than like a link in a
   sentence: this is the whole remedy for an evening with no films, and it is
   pressed by whoever is holding the phone, standing up, in the dark. */
#bannerAct {
  display: none; width: 100%; margin: 12px 0 2px; min-height: 64px;
  border-radius: 14px; background: var(--accent); color: #06121f;
  font-size: 20px; font-weight: 650;
  transition: transform .08s ease;
}
#bannerAct.on { display: block; }
#bannerAct:active { transform: scale(.97); }
/* The message and the button are separate elements so that setText() can keep
   its no-churn contract on the text while the button comes and goes. */
#bannerMsg { display: block; }
`;

/* A cold lamp takes the better part of a minute to strike, and for that minute
   this view is the only thing standing between a child and the conclusion that
   the appliance is broken -- which is the conclusion they act on. So it shows
   the film they picked, so they can see the right one is coming, says what is
   happening in words rather than in a spinner, and offers a way out. */
export const PREPARING_CSS = `
#preparing { text-align: center; }
#prepArt {
  width: min(50vw, 240px); aspect-ratio: 2 / 3; margin: 22px auto 0;
  border-radius: 14px; background: var(--card); object-fit: cover; display: block;
}
#prepTitle {
  font-size: 24px; font-weight: 700; margin: 18px 8px 6px; line-height: 1.2;
}
#prepStep { color: var(--accent); font-size: 19px; min-height: 27px; }
/* Indeterminate, and that is the honest choice rather than a missing feature.
   There is no percentage to report: the lamp answers when it answers, and a
   bar that crept to nine tenths and stopped would be a lie a child can see
   through -- the second time it happens they stop believing the screen. */
#prepBar {
  height: 6px; border-radius: 3px; background: #26262e;
  margin: 24px 4px 0; overflow: hidden;
}
#prepBar i {
  display: block; height: 100%; width: 38%; border-radius: 3px;
  background: var(--accent); animation: sweep 1.7s ease-in-out infinite;
}
@keyframes sweep {
  from { transform: translateX(-110%); }
  to   { transform: translateX(275%); }
}
@media (prefers-reduced-motion: reduce) {
  #prepBar i { animation: none; width: 100%; opacity: .45; }
}
/* Deliberately not the red STOP. Nothing has gone wrong and nothing is being
   interrupted -- this is changing your mind, and it should not look like an
   emergency stop on a film that has not started. */
#prepCancel {
  display: block; width: 100%; margin: 32px auto 0; max-width: 420px;
  min-height: 84px; border-radius: 20px; background: var(--card);
  color: var(--dim); font-size: 22px; font-weight: 650;
  transition: transform .08s ease;
}
#prepCancel:active { transform: scale(.97); }
`;

export const PLAYING_CSS = `
#playing { text-align: center; }
#nowTitle {
  font-size: 26px; font-weight: 700; margin: 26px 8px 4px; line-height: 1.2;
}
#nowSub { color: var(--dim); font-size: 17px; margin-bottom: 26px; }
#bar {
  position: relative; height: 6px; border-radius: 3px; background: #26262e;
  margin: 0 4px 10px;
}
#barFill {
  height: 100%; width: 0; background: var(--accent); border-radius: 3px;
  transition: width .8s linear;
}
/* Where a skip would land. Deliberately without #barFill's transition: this one
   is following a thumb, and easing it would make the number and the marker
   disagree about where the film is going. */
#barGhost {
  position: absolute; top: -5px; width: 3px; height: 16px; border-radius: 2px;
  background: var(--ink); display: none; margin-left: -1px;
}
#barGhost.on { display: block; }

/* Only ever on screen while a skip is pending, so the layout below it must not
   move when it appears -- hence a reserved line rather than a hidden element. */
#seekHint {
  min-height: 24px; margin: 0 4px 6px; font-size: 17px; font-weight: 650;
  color: var(--accent); font-variant-numeric: tabular-nums;
}

#transport {
  display: flex; align-items: center; justify-content: center; gap: min(4vw, 22px);
}
#toggle {
  width: min(42vw, 200px); aspect-ratio: 1; border-radius: 50%;
  background: var(--accent); color: #06121f;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: min(18vw, 88px); line-height: 1;
  transition: transform .08s ease;
  flex: 0 0 auto;
}
#toggle:active { transform: scale(.94); }

/* Big, because they are pressed and HELD by a child in the dark, and a hold
   that slips off the edge is a jump that does not happen. .held rather than
   :active: the button captures the pointer, so a thumb that slides off it is
   still holding it, and :active does not always agree. */
#skipBack, #skipFwd {
  width: min(24vw, 108px); aspect-ratio: 1; border-radius: 50%;
  background: var(--card); color: var(--ink);
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto; transition: transform .08s ease, background .12s ease;
}
#skipBack svg, #skipFwd svg { width: 58%; height: 58%; display: block; }
#skipBack.held, #skipFwd.held { background: #2f2f3a; transform: scale(.94); }

#stop {
  display: block; width: 100%; margin: 34px auto 0; max-width: 420px;
  min-height: 96px; border-radius: 20px; background: var(--stop);
  font-size: 26px; font-weight: 700; letter-spacing: .5px;
}
#stop:active { transform: scale(.97); }

#volume { display: none; margin: 24px auto 0; max-width: 420px; gap: 14px; }
#volume.on { display: flex; }
#volume button {
  flex: 1; min-height: 72px; border-radius: 18px; background: var(--card);
  font-size: 32px; font-weight: 700;
}
`;

export interface SwitcherViews {
  library: HTMLElement;
  preparing: HTMLElement;
  playing: HTMLElement;
  /** The funnel, built by the sheet manager: it belongs to the grid alone. */
  filterBtn: HTMLElement;
}

export class ViewSwitcher {
  private current: ViewName = "library";

  constructor(private readonly views: SwitcherViews) {
    this.paint();
  }

  get shown(): ViewName {
    return this.current;
  }

  show(name: ViewName): void {
    if (name === this.current) {
      return;
    }
    this.current = name;
    this.paint();
  }

  private paint(): void {
    const name = this.current;
    toggleClass(this.views.library, "on", name === "library");
    toggleClass(this.views.preparing, "on", name === "preparing");
    toggleClass(this.views.playing, "on", name === "playing");
    // The funnel belongs only to the grid: nothing on the now-playing view is a
    // list, so a control for narrowing one there would only be a mis-tap.
    toggleClass(this.views.filterBtn, "on", name === "library");
  }
}

/** An offer attached to a message: one label, one thing to do. Optional, and
 * absent for every banner that only reports. */
export interface BannerAction {
  label: string;
  onTap: () => void;
}

export class Banner {
  readonly root: HTMLElement;
  private readonly msg: HTMLElement;
  private readonly act: HTMLElement;
  private hold = 0;
  private tap: (() => void) | null = null;

  constructor(
    doc: Document,
    private readonly now: () => number = Date.now,
  ) {
    this.msg = h(doc, "span", { id: "bannerMsg" });
    // Built once and hidden, not created per message: the listener is attached
    // here and reads through the mutable `tap` below, so a banner that changes
    // what it offers does not leak a listener per poll.
    this.act = h(doc, "button", {
      id: "bannerAct",
      type: "button",
      onTap: () => this.tap?.(),
    });
    this.root = h(doc, "div", { id: "banner" }, [this.msg, this.act]);
  }

  /**
   * Show a message, or clear it with "". A hold keeps a deliberate message (a
   * refusal) up for its full time rather than letting the next status poll wipe
   * it a second later -- the one message that explains why nothing happened must
   * be readable.
   *
   * An action turns the message into an offer. Passing none clears any previous
   * one, so a banner cannot keep a button belonging to a situation that has
   * passed -- which for the NAS button would mean offering to switch on a
   * machine that is already running.
   */
  show(text: string, holdMs = 0, action?: BannerAction): void {
    if (text) {
      setText(this.msg, text);
      this.setAction(action);
      this.root.classList.add("on");
      if (holdMs) {
        this.hold = this.now() + holdMs;
      }
      return;
    }
    if (this.now() < this.hold) {
      return;
    }
    this.setAction(undefined);
    this.root.classList.remove("on");
  }

  private setAction(action?: BannerAction): void {
    this.tap = action ? action.onTap : null;
    if (action) {
      setText(this.act, action.label);
    }
    toggleClass(this.act, "on", !!action);
  }
}

export class PreparingView {
  readonly root: HTMLElement;
  /** Wired by the controls, which own the guard against a double tap. */
  readonly cancel: HTMLElement;
  private readonly title: HTMLElement;
  private readonly art: HTMLImageElement;
  private readonly step: HTMLElement;
  private artId = "";

  constructor(
    doc: Document,
    private readonly stamp: (path: string) => string,
  ) {
    this.art = h(doc, "img", { id: "prepArt", alt: "" }) as HTMLImageElement;
    this.title = h(doc, "div", { id: "prepTitle" });
    this.step = h(doc, "div", { id: "prepStep" });
    this.cancel = h(doc, "button", {
      id: "prepCancel",
      type: "button",
      text: "Never mind",
    });
    this.root = h(doc, "div", { id: "preparing", class: "view" }, [
      this.art,
      this.title,
      this.step,
      h(doc, "div", { id: "prepBar" }, [h(doc, "i")]),
      this.cancel,
    ]);
  }

  /** The tap that started a film, drawn before the POST answers so the right
   * poster appears immediately. */
  begin(title: string, id: string): void {
    setText(this.title, title);
    this.artId = id;
    this.art.src = this.stamp("/api/thumb/" + id);
    setText(this.step, "Getting ready\u2026");
  }

  /** A preparing status from the server (the film may have been picked on
   * another phone, so the title and poster come from the poll). */
  apply(s: Status): void {
    if (s.title) {
      setText(this.title, s.title);
    }
    if (s.id && this.artId !== s.id) {
      this.artId = s.id;
      this.art.src = this.stamp("/api/thumb/" + s.id);
    }
    // The server's wording, verbatim: the step and the sentence for it belong in
    // one place, and the copy nobody notices is wrong is the second one.
    setText(this.step, s.prepare?.label || "Getting ready\u2026");
  }
}

export class PlayingView {
  readonly root: HTMLElement;
  // The six the controls wire. They live here because they are drawn here.
  readonly toggle: HTMLElement;
  readonly stop: HTMLElement;
  readonly volDown: HTMLElement;
  readonly volUp: HTMLElement;
  readonly skipBack: HTMLElement;
  readonly skipFwd: HTMLElement;
  private readonly title: HTMLElement;
  private readonly sub: HTMLElement;
  private readonly bar: HTMLElement;
  private readonly ghost: HTMLElement;
  private readonly hint: HTMLElement;
  private readonly volume: HTMLElement;
  // The last poll's numbers, kept so a preview drawn between polls has
  // something to be relative to. The alternative -- handing the position to
  // showSeek() from the caller -- would put the presenter in the business of
  // knowing what a skip is.
  private position = 0;
  private duration = 0;

  constructor(doc: Document) {
    this.title = h(doc, "div", { id: "nowTitle" });
    this.sub = h(doc, "div", { id: "nowSub" });
    this.bar = h(doc, "div", { id: "barFill" });
    this.ghost = h(doc, "div", { id: "barGhost" });
    this.hint = h(doc, "div", { id: "seekHint" });
    this.toggle = h(doc, "button", {
      id: "toggle",
      aria: "Play or pause",
      html: PAUSE_GLYPH,
    });
    this.skipBack = h(doc, "button", {
      id: "skipBack",
      type: "button",
      aria: "Back ten seconds, or hold to go further",
      html: skipIcon(true),
    });
    this.skipFwd = h(doc, "button", {
      id: "skipFwd",
      type: "button",
      aria: "Forward ten seconds, or hold to go further",
      html: skipIcon(false),
    });
    this.stop = h(doc, "button", { id: "stop", text: "STOP" });
    this.volDown = h(doc, "button", { id: "volDown", aria: "Quieter", text: "\u2212" });
    this.volUp = h(doc, "button", { id: "volUp", aria: "Louder", text: "+" });
    this.volume = h(doc, "div", { id: "volume" }, [this.volDown, this.volUp]);
    this.root = h(doc, "div", { id: "playing", class: "view" }, [
      this.title,
      this.sub,
      h(doc, "div", { id: "bar" }, [this.bar, this.ghost]),
      this.hint,
      h(doc, "div", { id: "transport" }, [this.skipBack, this.toggle, this.skipFwd]),
      this.stop,
      this.volume,
    ]);
  }

  apply(s: Status): void {
    const paused = s.state === "paused";
    if (s.title) {
      setText(this.title, s.title);
    }
    this.toggle.innerHTML = paused ? PLAY_GLYPH : PAUSE_GLYPH;
    toggleClass(this.volume, "on", !!s.audio);
    const duration = s.duration ?? 0;
    const position = s.position ?? 0;
    this.duration = duration;
    this.position = position;
    if (duration > 0) {
      this.bar.style.width = barWidth(position, duration);
      setText(this.sub, paused ? "Paused" : timeLeft(duration - position));
    } else {
      setText(this.sub, paused ? "Paused" : "Playing");
    }
  }

  /**
   * Draw a skip that has not been sent yet: a marker on the bar where it would
   * land, and the jump and the destination in words. Zero clears it.
   *
   * The number shown is the CLAMPED one. A hold that has run past the end of
   * the film keeps climbing as a raw total, and a label that promised "+2:14:00"
   * for a jump the daemon is going to cut to eight minutes would be the page
   * lying about the one thing this control exists to make legible.
   */
  showSeek(pending: number): void {
    if (!pending || !(this.duration > 0)) {
      toggleClass(this.ghost, "on", false);
      setText(this.hint, "");
      return;
    }
    const limit = Math.max(0, this.duration - TAIL);
    const target = Math.max(0, Math.min(limit, this.position + pending));
    this.ghost.style.left = barWidth(target, this.duration);
    toggleClass(this.ghost, "on", true);
    setText(
      this.hint,
      jumpLabel(target - this.position) + " \u2192 " + clockTime(target),
    );
  }
}
