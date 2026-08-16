// The three views and the banner. Each one builds its own markup and keeps the
// handful of nodes it writes to, so there is no id lookup and no second file to
// keep in step; nothing here decides WHEN to show what -- that is the
// presenter's job -- so each of these is a set of small, order-free writes that
// a test can drive against the DOM stub and read straight back.
import { h, setText, toggleClass } from "./dom";
import { timeLeft, barWidth } from "./format";
import type { Status } from "./types";

export type ViewName = "library" | "preparing" | "playing";

const PLAY_GLYPH = "\u25B6"; // ▶
const PAUSE_GLYPH = "\u2759\u2759"; // ❙❙

export const BANNER_CSS = `
#banner {
  display: none; margin: 12px 0 4px; padding: 14px 16px; border-radius: 14px;
  background: #2a2317; color: #ffd79a; font-size: 17px; line-height: 1.35;
}
#banner.on { display: block; }
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
#bar { height: 6px; border-radius: 3px; background: #26262e; margin: 0 4px 30px; overflow: hidden; }
#barFill { height: 100%; width: 0; background: var(--accent); transition: width .8s linear; }

#toggle {
  width: min(46vw, 220px); aspect-ratio: 1; border-radius: 50%;
  background: var(--accent); color: #06121f;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: min(20vw, 96px); line-height: 1;
  transition: transform .08s ease;
}
#toggle:active { transform: scale(.94); }

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

export class Banner {
  readonly root: HTMLElement;
  private hold = 0;

  constructor(
    doc: Document,
    private readonly now: () => number = Date.now,
  ) {
    this.root = h(doc, "div", { id: "banner" });
  }

  /**
   * Show a message, or clear it with "". A hold keeps a deliberate message (a
   * refusal) up for its full time rather than letting the next status poll wipe
   * it a second later -- the one message that explains why nothing happened must
   * be readable.
   */
  show(text: string, holdMs = 0): void {
    if (text) {
      setText(this.root, text);
      this.root.classList.add("on");
      if (holdMs) {
        this.hold = this.now() + holdMs;
      }
      return;
    }
    if (this.now() < this.hold) {
      return;
    }
    this.root.classList.remove("on");
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
  // The four the controls wire. They live here because they are drawn here.
  readonly toggle: HTMLElement;
  readonly stop: HTMLElement;
  readonly volDown: HTMLElement;
  readonly volUp: HTMLElement;
  private readonly title: HTMLElement;
  private readonly sub: HTMLElement;
  private readonly bar: HTMLElement;
  private readonly volume: HTMLElement;

  constructor(doc: Document) {
    this.title = h(doc, "div", { id: "nowTitle" });
    this.sub = h(doc, "div", { id: "nowSub" });
    this.bar = h(doc, "div", { id: "barFill" });
    this.toggle = h(doc, "button", {
      id: "toggle",
      aria: "Play or pause",
      html: PAUSE_GLYPH,
    });
    this.stop = h(doc, "button", { id: "stop", text: "STOP" });
    this.volDown = h(doc, "button", { id: "volDown", aria: "Quieter", text: "\u2212" });
    this.volUp = h(doc, "button", { id: "volUp", aria: "Louder", text: "+" });
    this.volume = h(doc, "div", { id: "volume" }, [this.volDown, this.volUp]);
    this.root = h(doc, "div", { id: "playing", class: "view" }, [
      this.title,
      this.sub,
      h(doc, "div", { id: "bar" }, [this.bar]),
      this.toggle,
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
    if (duration > 0) {
      this.bar.style.width = barWidth(position, duration);
      setText(this.sub, paused ? "Paused" : timeLeft(duration - position));
    } else {
      setText(this.sub, paused ? "Paused" : "Playing");
    }
  }
}
