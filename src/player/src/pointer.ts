// The pointer: a dot over the film, moved from the phone.
//
// Two ways to move it. A touchpad, which works everywhere: drag on the pad and
// the dot moves like a laptop's cursor. And tilting, which only works where the
// browser hands the page the phone's orientation -- on HTTPS, and on iOS only
// after a tap that says yes. Tilting moves the dot only while "Hold to aim" is
// held, and starts from wherever the dot already is: re-centring on every press
// is what keeps the gyro's slow drift from ever mattering.
//
// How far a turn moves the dot is DEGREES_ACROSS and SMOOTHING by default. A
// phone that has been through the calibration wizard (calibrate.ts) has a
// PointerCalibration of its own instead: the turn and tilt it actually took to
// cross this picture from where this person sits, and the smoothing their hand
// needs. POINTER_CALIBRATION_DEFAULT is the constants exactly, so a phone with
// no profile aims as it always did.
//
// The daemon draws it (src/server/playstick/pointer.py). This sends at most
// TICK_MS apart, the newest sample only, one request in flight -- the same
// discipline the status poll keeps -- so a fast finger costs the Atom no more
// than a slow one.
import { h, head, setText } from "./dom";
import type { PostResult } from "./net";
import {
  type PointerPrefs,
  type StorageLike,
  loadPointerPrefs,
  savePointerPrefs,
} from "./prefs";
import type { Status } from "./types";

export const POINTER_CSS = `
#pointerPad {
  height: 46vh; border-radius: 18px; background: var(--card);
  border: 1px solid var(--line); touch-action: none;
  display: flex; align-items: center; justify-content: center; text-align: center;
  color: var(--dim); font-size: 16px; padding: 18px;
  -webkit-user-select: none; user-select: none;
}
#pointerPad.live { border-color: var(--accent); }
#aimBtn {
  display: none; width: 100%; min-height: 60px; margin-top: 12px;
  border-radius: 14px; font-size: 18px; font-weight: 650;
  background: var(--accent); color: #fff; touch-action: none;
  -webkit-user-select: none; user-select: none;
}
#aimBtn.on { display: block; }
#aimBtn.held { filter: brightness(1.2); }
.pickers { display: flex; gap: 8px; padding: 0 4px 10px; }
.pickers button {
  flex: 1; min-height: 48px; border-radius: 12px; font-size: 16px;
  background: var(--card); border: 1px solid var(--line); color: var(--ink);
}
.pickers button.sel { background: var(--accent); border-color: var(--accent); color: #fff; }
#pointerSpeed { width: 100%; margin: 4px 0 14px; }
#pointerNote { color: var(--dim); font-size: 15px; padding: 8px 4px 0; min-height: 1.4em; }
`;

/** The longest the page goes between samples while something is moving. */
export const TICK_MS = 40;
/** How long the dot stays after the finger lifts. */
export const LINGER_MS = 1000;
/** The screen's shape, for making a pad stroke move the same distance up as
 * across. The stick drives the projector at 16:9. */
export const SCREEN_ASPECT = 16 / 9;
/** Degrees of turn that sweep the whole width, at speed 1. */
export const DEGREES_ACROSS = 45;
/** How much of each new reading the smoothed aim takes, 0..1. */
export const SMOOTHING = 0.35;

export interface PointerCalibration {
  /** Degrees of turn from the left edge of the picture to the right. */
  yaw_across: number;
  /** Degrees of tilt from the top edge to the bottom. */
  pitch_across: number;
  /** 1, or -1 for a phone whose turn reads the other way. */
  yaw_sign: number;
  pitch_sign: number;
  /** How much of each new reading the smoothed aim takes, 0..1. */
  smoothing: number;
  /** Degrees of turn ignored at the start of an aim, for a shaky hand. */
  deadzone: number;
}

export const POINTER_CALIBRATION_DEFAULT: PointerCalibration = {
  yaw_across: DEGREES_ACROSS,
  pitch_across: DEGREES_ACROSS / SCREEN_ASPECT,
  yaw_sign: 1,
  pitch_sign: 1,
  smoothing: SMOOTHING,
  deadzone: 0,
};

export interface Sample {
  x: number;
  y: number;
  on: boolean;
  size: PointerPrefs["size"];
  color: PointerPrefs["color"];
}

export interface Point {
  x: number;
  y: number;
}

const clamp = (v: number): number => Math.min(1, Math.max(0, v));

/** Where a drag of (dx, dy) pixels on a pad `padWidth` wide moves the dot. */
export function padMove(from: Point, dx: number, dy: number, padWidth: number, speed: number): Point {
  const scale = speed / Math.max(1, padWidth);
  return { x: clamp(from.x + dx * scale), y: clamp(from.y + dy * scale * SCREEN_ASPECT) };
}

/** The shortest signed difference between two compass angles, -180..180. */
export function angleDelta(a: number, b: number): number {
  let d = (a - b) % 360;
  if (d > 180) {
    d -= 360;
  } else if (d < -180) {
    d += 360;
  }
  return d;
}

export interface Orientation {
  alpha: number;
  beta: number;
}

/**
 * Where the dot goes for a phone that has turned from `base` to `now` since
 * "Hold to aim" was pressed with the dot at `from`.
 *
 * Held like a remote -- screen up, top towards the projector -- alpha is the
 * turn left and right (it grows anticlockwise, so turning right shrinks it) and
 * beta is the tilt up and down (it grows as the top comes up).
 *
 * `cal` says how many degrees cross the picture each way; `speed` divides
 * them, so 2 crosses it in half the turn.
 */
export function aim(
  from: Point,
  base: Orientation,
  now: Orientation,
  speed: number,
  cal: PointerCalibration = POINTER_CALIBRATION_DEFAULT,
): Point {
  const yaw = cal.yaw_sign * deadzone(angleDelta(base.alpha, now.alpha), cal.deadzone);
  const pitch = cal.pitch_sign * deadzone(now.beta - base.beta, cal.deadzone);
  return {
    x: clamp(from.x + (yaw * speed) / cal.yaw_across),
    y: clamp(from.y - (pitch * speed) / cal.pitch_across),
  };
}

/** `v` with the first `dz` degrees either way taken off. */
function deadzone(v: number, dz: number): number {
  if (dz <= 0) {
    return v;
  }
  return Math.sign(v) * Math.max(0, Math.abs(v) - dz);
}

export interface SenderDeps {
  post: (s: Sample) => Promise<PostResult>;
  now: () => number;
  schedule: (fn: () => void, ms: number) => unknown;
  /** Told whether the daemon has a film to draw on. */
  onShown?: (shown: boolean) => void;
}

/** Newest sample only, one request in flight, at most one per TICK_MS. */
export class PointerSender {
  private pending: Sample | null = null;
  private inFlight = false;
  private timer = false;
  private last = -Infinity;
  sent = 0;

  constructor(private readonly deps: SenderDeps) {}

  send(sample: Sample): void {
    this.pending = sample;
    this.pump();
  }

  private pump(): void {
    if (this.inFlight || this.timer || !this.pending) {
      return;
    }
    const wait = this.last + TICK_MS - this.deps.now();
    if (wait > 0) {
      this.timer = true;
      this.deps.schedule(() => {
        this.timer = false;
        this.pump();
      }, wait);
      return;
    }
    const sample = this.pending;
    this.pending = null;
    this.inFlight = true;
    this.last = this.deps.now();
    this.sent++;
    const done = (): void => {
      this.inFlight = false;
      this.pump();
    };
    this.deps.post(sample).then(
      (r) => {
        this.deps.onShown?.(!!r.data.shown);
        done();
      },
      done,
    );
  }
}

export interface PointerSheetDeps {
  sender: PointerSender;
  storage: StorageLike | null;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  /** Whether the browser will hand the page the phone's orientation at all:
   * a secure context with DeviceOrientationEvent. */
  tiltAvailable: boolean;
  /** iOS: ask once, from a tap. Absent where no asking is needed. */
  requestTilt?: () => Promise<boolean>;
  /** Start listening; returns the function that stops. */
  listenTilt: (fn: (o: Orientation) => void) => () => void;
}

export class PointerSheet {
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly pad: HTMLElement;
  private readonly aimBtn: HTMLElement;
  private readonly note: HTMLElement;
  private readonly settings: HTMLElement;
  prefs: PointerPrefs;
  /** This phone's calibration, or null to aim with the defaults and the
   * speed slider. With one, the turn it measured is a full sweep and the
   * slider only drives the pad. */
  calibration: PointerCalibration | null = null;
  /** Where the dot is, 0..1 across and down. Kept between gestures. */
  dot: Point = { x: 0.5, y: 0.5 };
  private playing = false;
  private tiltAllowed = false;
  private drag: { x: number; y: number } | null = null;
  private aiming: { from: Point; base: Orientation | null; smooth: Point | null } | null = null;
  private stopTilt: (() => void) | null = null;
  private linger: number | null = null;

  constructor(doc: Document, private readonly deps: PointerSheetDeps) {
    this.doc = doc;
    this.prefs = loadPointerPrefs(deps.storage);
    this.tiltAllowed = deps.tiltAvailable && !deps.requestTilt;
    this.pad = h(doc, "div", { id: "pointerPad", text: "" });
    this.aimBtn = h(doc, "button", { id: "aimBtn", type: "button" });
    this.note = h(doc, "div", { id: "pointerNote" });
    this.settings = h(doc, "div", { id: "pointerSettings" });
    this.root = h(
      doc,
      "div",
      { id: "pointerSheet", class: "sheet", role: "dialog", modal: true, aria: "Pointer" },
      [head(doc, "Pointer"), this.pad, this.aimBtn, this.note, head(doc, "Settings"), this.settings],
    );
    this.wirePad();
    this.wireAim();
  }

  onStatus(s: Status): void {
    this.playing = s.state === "playing" || s.state === "paused";
  }

  get playingNow(): boolean {
    return this.playing;
  }

  paint(): void {
    setText(
      this.pad,
      this.playing
        ? "Drag here to move the dot"
        : "The pointer draws over a film. Start one first.",
    );
    const tilt = this.deps.tiltAvailable && this.prefs.tilt;
    this.aimBtn.classList.toggle("on", tilt && this.playing);
    setText(this.aimBtn, this.tiltAllowed ? "Hold to aim" : "Allow tilting");
    this.paintSettings();
  }

  /** The sheet closed: hide the dot rather than leave it on the film. */
  closed(): void {
    this.endAim();
    this.drag = null;
    this.hide();
  }

  // --- sending ------------------------------------------------------------

  private sample(on: boolean): Sample {
    return { x: this.dot.x, y: this.dot.y, on, size: this.prefs.size, color: this.prefs.color };
  }

  private show(): void {
    if (this.linger !== null) {
      this.deps.cancel(this.linger);
      this.linger = null;
    }
    this.deps.sender.send(this.sample(true));
  }

  private hideSoon(): void {
    if (this.linger !== null) {
      this.deps.cancel(this.linger);
    }
    this.linger = this.deps.schedule(() => {
      this.linger = null;
      this.hide();
    }, LINGER_MS);
  }

  private hide(): void {
    this.deps.sender.send(this.sample(false));
  }

  // --- the pad ------------------------------------------------------------

  private wirePad(): void {
    const pos = (ev: unknown): { x: number; y: number } => {
      const e = ev as { clientX?: number; clientY?: number };
      return { x: e.clientX ?? 0, y: e.clientY ?? 0 };
    };
    this.pad.addEventListener("pointerdown", (ev) => {
      if (!this.playing) {
        return;
      }
      (ev as { preventDefault?: () => void }).preventDefault?.();
      const target = this.pad as HTMLElement & { setPointerCapture?: (id: number) => void };
      const id = (ev as { pointerId?: number }).pointerId;
      if (id !== undefined) {
        target.setPointerCapture?.(id);
      }
      this.drag = pos(ev);
      this.pad.classList.add("live");
      this.show();
    });
    this.pad.addEventListener("pointermove", (ev) => {
      if (!this.drag) {
        return;
      }
      const p = pos(ev);
      const width = (this.pad as HTMLElement).clientWidth || 300;
      this.dot = padMove(this.dot, p.x - this.drag.x, p.y - this.drag.y, width, this.prefs.speed);
      this.drag = p;
      this.show();
    });
    const end = (): void => {
      if (!this.drag) {
        return;
      }
      this.drag = null;
      this.pad.classList.remove("live");
      this.hideSoon();
    };
    this.pad.addEventListener("pointerup", end);
    this.pad.addEventListener("pointercancel", end);
  }

  // --- tilting ------------------------------------------------------------

  private wireAim(): void {
    // A click, not a press, for the permission: iOS only asks from one.
    this.aimBtn.addEventListener("click", () => {
      if (this.tiltAllowed || !this.deps.requestTilt) {
        return;
      }
      void this.deps.requestTilt().then((ok) => {
        this.tiltAllowed = ok;
        setText(this.note, ok ? "" : "Tilting was not allowed. The pad still works.");
        this.paint();
      });
    });
    this.aimBtn.addEventListener("pointerdown", (ev) => {
      if (!this.tiltAllowed || !this.playing) {
        return;
      }
      (ev as { preventDefault?: () => void }).preventDefault?.();
      this.aimBtn.classList.add("held");
      this.aiming = { from: { ...this.dot }, base: null, smooth: null };
      this.stopTilt = this.deps.listenTilt((o) => this.onTilt(o));
      this.show();
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
      this.aimBtn.addEventListener(type, () => {
        if (this.aiming) {
          this.endAim();
          this.hideSoon();
        }
      });
    }
  }

  /** One orientation reading while "Hold to aim" is held. */
  onTilt(o: Orientation): void {
    const a = this.aiming;
    if (!a) {
      return;
    }
    if (!a.base) {
      a.base = o;
      return;
    }
    const cal = this.calibration;
    const target = cal ? aim(a.from, a.base, o, 1, cal) : aim(a.from, a.base, o, this.prefs.speed);
    const k = cal ? cal.smoothing : SMOOTHING;
    a.smooth = a.smooth
      ? {
          x: a.smooth.x + (target.x - a.smooth.x) * k,
          y: a.smooth.y + (target.y - a.smooth.y) * k,
        }
      : target;
    this.dot = a.smooth;
    this.show();
  }

  private endAim(): void {
    this.aimBtn.classList.remove("held");
    this.aiming = null;
    if (this.stopTilt) {
      this.stopTilt();
      this.stopTilt = null;
    }
  }

  // --- settings -----------------------------------------------------------

  private paintSettings(): void {
    const doc = this.doc;
    this.settings.textContent = "";
    const speed = h(doc, "input", { id: "pointerSpeed", aria: "Speed" }) as HTMLInputElement;
    speed.type = "range";
    speed.min = "0.5";
    speed.max = "3";
    speed.step = "0.25";
    speed.value = String(this.prefs.speed);
    speed.addEventListener("input", () => this.set({ speed: Number(speed.value) }, false));
    this.settings.appendChild(h(doc, "div", { class: "sub", text: "Speed" }));
    this.settings.appendChild(speed);

    const picker = (
      options: readonly [string, string][],
      current: string,
      pick: (v: string) => void,
    ): HTMLElement =>
      h(
        doc,
        "div",
        { class: "pickers" },
        options.map(([value, label]) =>
          h(doc, "button", {
            type: "button",
            class: value === current ? "sel" : "",
            text: label,
            onTap: () => pick(value),
          }),
        ),
      );
    this.settings.appendChild(
      picker(
        [["s", "Small"], ["m", "Medium"], ["l", "Large"]],
        this.prefs.size,
        (v) => this.set({ size: v as PointerPrefs["size"] }),
      ),
    );
    this.settings.appendChild(
      picker(
        [["red", "Red"], ["green", "Green"], ["white", "White"]],
        this.prefs.color,
        (v) => this.set({ color: v as PointerPrefs["color"] }),
      ),
    );
    if (this.deps.tiltAvailable && this.calibration) {
      this.settings.appendChild(
        h(doc, "div", {
          class: "sub",
          text: "Tilting uses this phone's calibration. Speed is for the pad.",
        }),
      );
    }
    if (this.deps.tiltAvailable) {
      this.settings.appendChild(
        picker(
          [["on", "Tilt to aim"], ["off", "Pad only"]],
          this.prefs.tilt ? "on" : "off",
          (v) => this.set({ tilt: v === "on" }),
        ),
      );
    } else {
      this.settings.appendChild(
        h(doc, "div", {
          class: "sub",
          text: "Tilting to aim needs the page opened over https://.",
        }),
      );
    }
  }

  private set(change: Partial<PointerPrefs>, repaint = true): void {
    this.prefs = { ...this.prefs, ...change };
    savePointerPrefs(this.deps.storage, this.prefs);
    if (repaint) {
      this.paint();
    }
  }
}

