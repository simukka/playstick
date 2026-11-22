// The calibration wizard's arithmetic. Pure: readings in, a profile out, so
// every number the wizard settles on is tested without a phone.
//
// WHAT IS MEASURED
//
// The pointer. The person points the top of the phone at each edge of the
// picture in turn and holds still for a moment. The left and right holds give
// the turn that crosses the picture's width; top and bottom give the tilt
// that crosses its height. Which way the numbers went says the sign. How much
// the readings wobbled during each hold is the hand's shake, which sets the
// smoothing and a small deadzone. The midpoint of the four is where the
// middle of the picture is, which the hit-zone step aims from.
//
// The controller. Held sideways like a gamepad, the phone is tilted each way
// as far as is comfortable and brought back to the middle. The press point
// for each direction is PRESS_SHARE of that peak, so a person who tilts a
// little gets a pad that answers to a little. If the first tilt on an axis
// went the opposite way to the one asked for, the axis is flipped.
//
// The buttons. Assigned by tapping, one at a time; see ButtonPicker.
//
// Every limit here matches the one profiles.py checks on the stick, so a
// profile this builds is one the stick will keep.
import { type ButtonMap, type ButtonName, BUTTON_NAMES, BUTTONS_DEFAULT } from "./gamepad";
import {
  POINTER_CALIBRATION_DEFAULT,
  SCREEN_ASPECT,
  angleDelta,
  type Orientation,
  type PointerCalibration,
  type Point,
} from "./pointer";
import { TILT_CALIBRATION_DEFAULT, type TiltCalibration } from "./tilt-pad";

export interface Checks {
  /** How far off each hit-zone target the dot was, as a fraction of the
   * picture's width. 1 for a target that was skipped. */
  targets: number[];
  /** Every button was given a job. */
  buttons_ok: boolean;
}

export interface Profile {
  name: string;
  pointer: PointerCalibration;
  tilt: TiltCalibration;
  buttons: ButtonMap;
  checks: Checks;
  /** Set by the stick on a profile it has stored. */
  id?: string;
  updated_at?: number;
}

export const DEFAULT_PROFILE: Profile = {
  name: "",
  pointer: POINTER_CALIBRATION_DEFAULT,
  tilt: TILT_CALIBRATION_DEFAULT,
  buttons: BUTTONS_DEFAULT,
  checks: { targets: [], buttons_ok: false },
};

// --- limits, as profiles.py has them -------------------------------------

export const LIMITS = {
  yaw_across: [10, 120],
  pitch_across: [5, 90],
  smoothing: [0, 0.9],
  deadzone: [0, 5],
  press: [4, 45],
  release_ratio: [0.3, 0.95],
} as const;

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));
const round = (v: number, places = 2): number => Math.round(v * 10 ** places) / 10 ** places;

// --- holding still -------------------------------------------------------

/** A hold: the mean reading, and how much it wobbled (degrees, RMS). */
export interface Hold {
  mean: Orientation;
  jitter: number;
}

/** Average a hold's readings. Alpha is a compass angle, so it is averaged as
 * offsets from the first reading -- a hold across north would otherwise
 * average to south. */
export function summarise(samples: readonly Orientation[]): Hold | null {
  if (!samples.length) {
    return null;
  }
  const first = samples[0]!.alpha;
  const da = samples.map((s) => angleDelta(s.alpha, first));
  const db = samples.map((s) => s.beta);
  const ma = da.reduce((a, b) => a + b, 0) / samples.length;
  const mb = db.reduce((a, b) => a + b, 0) / samples.length;
  const va = da.reduce((a, d) => a + (d - ma) ** 2, 0) / samples.length;
  const vb = db.reduce((a, d) => a + (d - mb) ** 2, 0) / samples.length;
  return {
    mean: { alpha: (((first + ma) % 360) + 360) % 360, beta: mb },
    jitter: Math.sqrt((va + vb) / 2),
  };
}

// --- the pointer ---------------------------------------------------------

export type Edge = "left" | "right" | "top" | "bottom";
export const EDGES: readonly Edge[] = ["left", "right", "top", "bottom"];

export interface PointerFit {
  cal: PointerCalibration;
  /** Where the phone points at the middle of the picture. */
  centre: Orientation;
  /** The hand's shake, averaged over the four holds. */
  jitter: number;
}

/** The pointer calibration from four edge holds, or a sentence saying what
 * to do differently. */
export function fitPointer(edges: Record<Edge, Hold>): PointerFit | { error: string } {
  // As aim() measures it: from the left edge to the right, base minus now.
  const yaw = angleDelta(edges.left.mean.alpha, edges.right.mean.alpha);
  const pitch = edges.top.mean.beta - edges.bottom.mean.beta;
  const [ylo, yhi] = LIMITS.yaw_across;
  const [plo, phi] = LIMITS.pitch_across;
  if (Math.abs(yaw) < ylo) {
    return { error: "The phone hardly turned between the left and right edges. Try again, pointing right at each edge." };
  }
  if (Math.abs(yaw) > yhi) {
    return { error: "That was more than a third of a circle between the edges. Try again from where you will sit." };
  }
  if (Math.abs(pitch) < plo) {
    return { error: "The phone hardly tilted between the top and bottom edges. Try again, pointing right at each edge." };
  }
  if (Math.abs(pitch) > phi) {
    return { error: "That was a very big tilt between the top and bottom. Try again from where you will sit." };
  }
  const jitter =
    (edges.left.jitter + edges.right.jitter + edges.top.jitter + edges.bottom.jitter) / 4;
  const cal: PointerCalibration = {
    yaw_across: round(Math.abs(yaw), 1),
    pitch_across: round(Math.abs(pitch), 1),
    yaw_sign: yaw >= 0 ? 1 : -1,
    pitch_sign: pitch >= 0 ? 1 : -1,
    // A steady hand gets a quick dot, a shaky one a calmer dot.
    smoothing: round(clamp(0.5 - jitter * 0.35, 0.15, 0.6)),
    deadzone: round(clamp(jitter * 1.5, 0, 2)),
  };
  const centreAlpha = edges.right.mean.alpha + angleDelta(edges.left.mean.alpha, edges.right.mean.alpha) / 2;
  return {
    cal,
    centre: {
      alpha: ((centreAlpha % 360) + 360) % 360,
      beta: (edges.top.mean.beta + edges.bottom.mean.beta) / 2,
    },
    jitter,
  };
}

// --- hit zones -----------------------------------------------------------

export interface Target {
  x: number;
  y: number;
  /** Radius, as a fraction of the picture's height. */
  r: number;
}

export const TARGET_R = 0.06;
// The corners sit clear of the instructions along the top of the projector
// and the telemetry along the bottom (calibrate.py draws both).
export const TARGETS: readonly Target[] = [
  { x: 0.5, y: 0.5, r: TARGET_R },
  { x: 0.12, y: 0.32, r: TARGET_R },
  { x: 0.88, y: 0.32, r: TARGET_R },
  { x: 0.88, y: 0.74, r: TARGET_R },
  { x: 0.12, y: 0.74, r: TARGET_R },
];
/** How long the dot has to stay inside a target to hit it. */
export const TARGET_HOLD_MS = 600;
/** A mean miss above this suggests doing the sweep again. */
export const TARGET_REDO = 0.05;

/** How far `p` is from the middle of `t`, as a fraction of the width. */
export function miss(p: Point, t: Target): number {
  return Math.hypot(p.x - t.x, (p.y - t.y) / SCREEN_ASPECT);
}

/** Whether `p` is inside `t`. The radius is in heights, so x is scaled. */
export function inside(p: Point, t: Target): boolean {
  return Math.hypot((p.x - t.x) * SCREEN_ASPECT, p.y - t.y) <= t.r;
}

// --- the controller ------------------------------------------------------

export type Direction = "left" | "right" | "up" | "down";
export const DIRECTIONS: readonly Direction[] = ["left", "right", "up", "down"];
/** The press point, as a share of the comfortable peak. */
export const PRESS_SHARE = 0.6;
/** A tilt has to pass this to count as one at all. */
export const CAPTURE_MIN_DEG = 8;
/** ...and come back within this of level to be finished. */
export const CAPTURE_BACK_DEG = 3;

/** How far along `dir` an offset from TiltPad is: roll positive is LEFT,
 * pitch negative is UP. */
export function along(dir: Direction, offset: { roll: number; pitch: number }): number {
  switch (dir) {
    case "left":
      return offset.roll;
    case "right":
      return -offset.roll;
    case "up":
      return -offset.pitch;
    default:
      return offset.pitch;
  }
}

export interface Captured {
  /** The furthest the tilt went, degrees. */
  peak: number;
  /** It went the other way from the one asked for. */
  flipped: boolean;
}

/** Watches one "tilt this way and come back". */
export class TiltCapture {
  private hi = 0;
  private lo = 0;

  constructor(readonly dir: Direction) {}

  /** Progress so far, 0..1, for the projector's ring. */
  get progress(): number {
    return clamp(Math.max(this.hi, -this.lo) / CAPTURE_MIN_DEG, 0, 1);
  }

  /** One offset in. Done once a tilt past CAPTURE_MIN_DEG has come back. */
  update(offset: { roll: number; pitch: number }): Captured | null {
    const v = along(this.dir, offset);
    this.hi = Math.max(this.hi, v);
    this.lo = Math.min(this.lo, v);
    const peak = Math.max(this.hi, -this.lo);
    if (peak < CAPTURE_MIN_DEG || Math.abs(v) > CAPTURE_BACK_DEG) {
      return null;
    }
    return this.hi >= -this.lo ? { peak: this.hi, flipped: false } : { peak: -this.lo, flipped: true };
  }
}

/** The press point for a comfortable peak. */
export function pressFor(peak: number): number {
  return round(clamp(peak * PRESS_SHARE, 6, 30), 1);
}

// --- the buttons ---------------------------------------------------------

/** Hands out jobs to the on-screen buttons, one tap at a time: "tap the one
 * you want as A", then B, START and SELECT. */
export class ButtonPicker {
  /** Which job each on-screen button has been given so far. */
  readonly given: Partial<Record<ButtonName, ButtonName>> = {};
  private index = 0;

  /** The job being asked for, or null once all four are given. */
  get wanted(): ButtonName | null {
    return BUTTON_NAMES[this.index] ?? null;
  }

  /** Button `pressed` was tapped. Returns the job it already had, if it had
   * one -- a second tap on it does not take a second job. */
  tap(pressed: ButtonName): { already?: ButtonName } {
    const had = this.given[pressed];
    if (had) {
      return { already: had };
    }
    const want = this.wanted;
    if (want) {
      this.given[pressed] = want;
      this.index++;
    }
    return {};
  }

  get done(): boolean {
    return this.wanted === null;
  }

  /** The finished map: each on-screen button to the bit of its job. */
  map(): ButtonMap {
    const out = { ...BUTTONS_DEFAULT };
    for (const name of BUTTON_NAMES) {
      const job = this.given[name];
      if (job) {
        out[name] = BUTTONS_DEFAULT[job];
      }
    }
    return out;
  }
}

/** The same map with A and B the other way round. */
export function swapAB(map: ButtonMap): ButtonMap {
  const out = { ...map };
  for (const name of BUTTON_NAMES) {
    if (map[name] === BUTTONS_DEFAULT.A) {
      out[name] = BUTTONS_DEFAULT.B;
    } else if (map[name] === BUTTONS_DEFAULT.B) {
      out[name] = BUTTONS_DEFAULT.A;
    }
  }
  return out;
}

// --- reading a profile back ----------------------------------------------

const num = (v: unknown, lo: number, hi: number): number | null =>
  typeof v === "number" && Number.isFinite(v) && v >= lo && v <= hi ? v : null;
const sign = (v: unknown): number | null => (v === 1 || v === -1 ? v : null);

/** A profile from the stick or from storage, checked, or null if any part of
 * it is not one. All or nothing: half a profile would be a pointer nobody
 * calibrated. */
export function parseProfile(raw: unknown): Profile | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const r = raw as Record<string, unknown>;
  const p = (r["pointer"] ?? {}) as Record<string, unknown>;
  const t = (r["tilt"] ?? {}) as Record<string, unknown>;
  const press = (t["press"] ?? {}) as Record<string, unknown>;
  const b = (r["buttons"] ?? {}) as Record<string, unknown>;
  const pointer = {
    yaw_across: num(p["yaw_across"], ...LIMITS.yaw_across),
    pitch_across: num(p["pitch_across"], ...LIMITS.pitch_across),
    yaw_sign: sign(p["yaw_sign"]),
    pitch_sign: sign(p["pitch_sign"]),
    smoothing: num(p["smoothing"], ...LIMITS.smoothing),
    deadzone: num(p["deadzone"], ...LIMITS.deadzone),
  };
  const presses = {
    left: num(press["left"], ...LIMITS.press),
    right: num(press["right"], ...LIMITS.press),
    up: num(press["up"], ...LIMITS.press),
    down: num(press["down"], ...LIMITS.press),
  };
  const tilt = {
    release_ratio: num(t["release_ratio"], ...LIMITS.release_ratio),
    roll_sign: sign(t["roll_sign"]),
    pitch_sign: sign(t["pitch_sign"]),
  };
  const bits = Object.values(BUTTONS_DEFAULT);
  const buttons = {} as ButtonMap;
  for (const name of BUTTON_NAMES) {
    const v = b[name];
    if (typeof v !== "number" || !bits.includes(v)) {
      return null;
    }
    buttons[name] = v;
  }
  if (new Set(Object.values(buttons)).size !== BUTTON_NAMES.length) {
    return null;
  }
  if ([...Object.values(pointer), ...Object.values(presses), ...Object.values(tilt)].some((v) => v === null)) {
    return null;
  }
  const checks = (r["checks"] ?? {}) as Record<string, unknown>;
  const targets = Array.isArray(checks["targets"])
    ? (checks["targets"] as unknown[]).filter((v): v is number => typeof v === "number")
    : [];
  return {
    name: typeof r["name"] === "string" ? r["name"] : "",
    pointer: pointer as PointerCalibration,
    tilt: { ...(tilt as Omit<TiltCalibration, "press">), press: presses as TiltCalibration["press"] },
    buttons,
    checks: { targets, buttons_ok: !!checks["buttons_ok"] },
    ...(typeof r["id"] === "string" ? { id: r["id"] } : {}),
    ...(typeof r["updated_at"] === "number" ? { updated_at: r["updated_at"] } : {}),
  };
}

/** The body the stick's POST /api/profile/<id> takes. */
export function profileBody(p: Profile): Omit<Profile, "id" | "updated_at"> {
  return { name: p.name, pointer: p.pointer, tilt: p.tilt, buttons: p.buttons, checks: p.checks };
}

/** A fresh phone id: random, and nothing else. */
export function makePhoneId(random: () => number = Math.random): string {
  const hex = (n: number): string =>
    Array.from({ length: n }, () => Math.floor(random() * 16).toString(16)).join("");
  return "phone-" + hex(16);
}
