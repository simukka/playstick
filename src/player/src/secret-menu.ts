// The menu hidden in the Playstick wordmark: the projector (and, behind the gear
// beside it, the projector's own settings), the clock, the pointer, and the calibration wizard for the pointer and the gamepad. For grown-ups, so it is not where a child's thumb lands by accident.
//
// HOW IT OPENS. A long press on a touch screen, because a tap is what a child
// does to everything; a plain click with a mouse, because nobody long-presses a
// mouse and a laptop is a grown-up's anyway. Nothing on the logo says it does
// anything -- no cursor change, no hint -- and it keeps its "Playstick" label.
//
// This hides the menu; it does not secure it. Every route behind it is as open
// as play and stop are, to anybody on the allowed network.
import { h, head, grip, setText } from "./dom";
import { row } from "./sheet";
import type { PostResult } from "./net";
import type { PlayerState, Projector, Status } from "./types";

export const SECRET_CSS = `
.brand { -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
#playing .brand { margin: 0 0 14px; }
#playing .brand svg { display: block; height: 20px; width: auto; }
.row.act::after { display: none; }
.row.off { opacity: .55; }
/* The power row and the gear that opens the projector's settings share a
   line: the power row stands alone, rounded, and the gear is a square beside
   it. Two buttons rather than one inside the other, which HTML forbids. */
.projLine { display: flex; gap: 10px; margin-bottom: 10px; }
.projLine .row { flex: 1; min-width: 0; border-radius: 14px; }
.projLine .gear {
  flex: none; width: 60px; min-height: 60px; border-radius: 14px;
  background: var(--card); color: var(--ink);
  display: inline-flex; align-items: center; justify-content: center;
}
.projLine .gear:active { background: var(--card2); }
.projLine .gear svg { width: 26px; height: 26px; display: block; }
#secretNote {
  color: var(--dim); font-size: 16px; line-height: 1.45; padding: 10px 14px 2px;
  min-height: 1.45em;
}
`;

const GEAR_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/>' +
  '<circle cx="12" cy="12" r="6.5"/>' +
  '<path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1' +
  'M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/></svg>';

/** How long a finger has to stay on the logo. */
export const HOLD_MS = 700;
/** How far it may wander and still count as holding. */
export const HOLD_SLOP_PX = 10;

export interface HoldDeps {
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
}

interface PointerLike {
  pointerType?: string;
  clientX?: number;
  clientY?: number;
  preventDefault?: () => void;
}

/**
 * Open `open` on a long press (touch, pen) or a click (mouse).
 *
 * The click a touch screen sends after a short tap is ignored -- the last
 * pointerdown said what kind of pointer this is -- so tapping the logo does
 * nothing, which is the point.
 */
export function holdToOpen(el: HTMLElement, open: () => void, deps: HoldDeps): void {
  let timer: number | null = null;
  let kind = "";
  let x = 0;
  let y = 0;
  const stop = (): void => {
    if (timer !== null) {
      deps.cancel(timer);
      timer = null;
    }
  };
  el.addEventListener("pointerdown", (ev) => {
    const e = ev as PointerLike;
    kind = e.pointerType || "mouse";
    if (kind === "mouse") {
      return;
    }
    x = e.clientX ?? 0;
    y = e.clientY ?? 0;
    stop();
    timer = deps.schedule(() => {
      timer = null;
      open();
    }, HOLD_MS);
  });
  el.addEventListener("pointermove", (ev) => {
    const e = ev as PointerLike;
    if (timer === null) {
      return;
    }
    if (Math.hypot((e.clientX ?? x) - x, (e.clientY ?? y) - y) > HOLD_SLOP_PX) {
      stop();
    }
  });
  for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
    el.addEventListener(type, stop);
  }
  el.addEventListener("click", () => {
    if (kind === "mouse" || kind === "") {
      open();
    }
  });
  // The long press is ours: no "copy image" menu, no text selection.
  el.addEventListener("contextmenu", (ev) => {
    (ev as PointerLike).preventDefault?.();
  });
}

export interface SecretSheetDeps {
  power: (on: boolean) => Promise<PostResult>;
  wakeClock: () => Promise<PostResult>;
  openPointer: () => void;
  openCalibrate: () => void;
  /** The projector's own settings: picture mode, input, and the rest. */
  openProjector: () => void;
}

export class SecretSheet {
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly list: HTMLElement;
  private readonly note: HTMLElement;
  private projector: Projector = { model: "none", power: "unknown", fault: "" };
  private state: PlayerState = "idle";
  private said = "";

  constructor(doc: Document, private readonly deps: SecretSheetDeps) {
    this.doc = doc;
    this.list = h(doc, "div", { id: "secretList" });
    this.note = h(doc, "div", { id: "secretNote" });
    this.root = h(
      doc,
      "div",
      { id: "secretSheet", class: "sheet", role: "dialog", modal: true, aria: "Settings" },
      [grip(doc), head(doc, "Playstick"), this.list, this.note],
    );
  }

  onStatus(s: Status): void {
    this.state = s.state;
    if (s.projector) {
      this.projector = s.projector;
    }
  }

  /** Called when the sheet opens: whatever the last action said is stale. */
  opened(): void {
    this.said = "";
    this.paint();
  }

  paint(): void {
    this.list.textContent = "";
    const film =
      this.state === "playing" || this.state === "paused" || this.state === "preparing";

    if (this.projector.model !== "none") {
      const lit = this.projector.power === "on";
      const blocked = lit && film;
      const sub = this.projector.fault
        ? this.projector.fault
        : blocked
          ? "Stop the film first"
          : this.projector.power === "unknown"
            ? "It isn't saying whether it's on"
            : "";
      const power = this.action(
        lit ? "Turn projector off" : "Turn projector on",
        sub,
        blocked,
        () => this.run(this.deps.power(!lit)),
      );
      const gear = h(this.doc, "button", {
        class: "gear",
        type: "button",
        aria: "Projector settings",
        html: GEAR_ICON,
        onTap: () => this.deps.openProjector(),
      });
      this.list.appendChild(h(this.doc, "div", { class: "projLine" }, [power, gear]));
    }

    const busy = film || this.state === "airplay";
    const clockSub = busy
      ? "The clock shows when nothing is playing"
      : this.projector.model !== "none" && this.projector.power === "standby"
        ? "The projector is off, so turn it on to see it"
        : "";
    this.list.appendChild(
      this.action("Show clock", clockSub, busy, () => this.run(this.deps.wakeClock())),
    );

    this.list.appendChild(
      this.action("Pointer", "Point at things on the screen", false, () =>
        this.deps.openPointer(),
      ),
    );

    const calBusy = film || this.state === "airplay" || this.state === "gaming";
    this.list.appendChild(
      this.action(
        "Calibrate phone",
        calBusy
          ? "Stop what is on the projector first"
          : "Set up the pointer and gamepad for this phone",
        calBusy,
        () => this.deps.openCalibrate(),
      ),
    );
    setText(this.note, this.said);
  }

  private action(label: string, sub: string, off: boolean, fn: () => void): HTMLElement {
    const el = row(this.doc, label, sub, false, () => {
      if (off) {
        this.say(sub);
        return;
      }
      fn();
    });
    el.classList.add("act");
    if (off) {
      el.classList.add("off");
      el.setAttribute("aria-disabled", "true");
    }
    return el;
  }

  private run(p: Promise<PostResult>): void {
    this.say("…");
    void p.then(
      (r) => {
        const message = r.data.error || (r.data.message as string | undefined) || "Done.";
        if (r.ok && r.data.projector) {
          this.projector = r.data.projector as Projector;
        }
        this.said = message;
        this.paint();
      },
      () => this.say("The stick did not answer."),
    );
  }

  private say(text: string): void {
    this.said = text;
    setText(this.note, text);
  }
}
