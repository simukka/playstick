// The calibration wizard: a sheet on the phone, a black screen on the projector.
//
// Opened from the logo's hidden menu. It puts mpv on the projector with nothing
// loaded (POST /api/calibrate/start) and, from then on, streams what it is
// doing and the phone's raw readings to the stick (calibrate-link.ts), which
// draws them (src/server/playstick/calibrate.py). The person looks at the
// projector, not at the phone: that is where the targets, the edge being asked
// for and the lit buttons are.
//
// The steps, in order:
//
//   intro    name this phone's profile, or pick one the stick already has
//   starting the projector warming up, as for a film
//   sweep    point at each edge of the picture and hold: the pointer's spans
//   targets  aim the dot at five circles: whether the spans are right
//   tilt     hold it like a gamepad and tilt each way: the d-pad's press points
//   buttons  tap the on-screen button for A, B, START, SELECT: the remap
//   summary  the numbers, and Save
//
// The arithmetic is all in calibration.ts. This file is the order of the steps
// and the buttons that move between them; every reading goes through
// onReading(), which is public so the tests can be the phone.
import { h, head, grip, setText } from "./dom";
import {
  type ButtonMap,
  type ButtonName,
  BUTTONS_DEFAULT,
  bitName,
} from "./gamepad";
import type { CalibrateLink, Scene, Telemetry } from "./calibrate-link";
import {
  ButtonPicker,
  DIRECTIONS,
  type Direction,
  EDGES,
  type Edge,
  type Hold,
  type PointerFit,
  type Profile,
  TARGETS,
  TARGET_HOLD_MS,
  TARGET_REDO,
  TiltCapture,
  fitPointer,
  inside,
  miss,
  parseProfile,
  pressFor,
  profileBody,
  summarise,
  swapAB,
} from "./calibration";
import type { PostResult } from "./net";
import { aim, type Orientation, type Point } from "./pointer";
import { TILT_CALIBRATION_DEFAULT, TiltPad, type TiltCalibration } from "./tilt-pad";
import type { Status } from "./types";

export const CALIBRATE_CSS = `
#calBody p { color: var(--dim); font-size: 16px; line-height: 1.45; margin: 6px 4px 14px; }
#calBody p.lead { color: var(--ink); font-size: 18px; }
#calName {
  width: 100%; min-height: 52px; border-radius: 12px; padding: 0 14px;
  font: inherit; font-size: 18px; color: var(--ink); background: var(--card);
  border: 1px solid var(--line); margin: 0 0 14px;
  -webkit-user-select: text; user-select: text;
}
.calGo, .calHold {
  display: block; width: 100%; min-height: 60px; margin: 8px 0;
  border-radius: 14px; font-size: 18px; font-weight: 650;
  background: var(--accent); color: #fff; touch-action: none;
  -webkit-user-select: none; user-select: none;
}
.calHold { min-height: 120px; font-size: 20px; }
.calHold.held { filter: brightness(1.25); }
.calGo:disabled { opacity: .45; }
.calAlt {
  display: block; width: 100%; min-height: 48px; margin: 8px 0;
  border-radius: 12px; font-size: 16px; background: var(--card);
  border: 1px solid var(--line); color: var(--ink);
}
#calLive { color: var(--dim); font-size: 15px; padding: 4px 4px 0; min-height: 1.4em;
  font-variant-numeric: tabular-nums; }
#calNote { color: #ffcf8a; font-size: 16px; padding: 8px 4px 0; min-height: 1.4em; }
#calPad { display: flex; gap: 10px; align-items: center; margin: 8px 0; touch-action: none; }
#calPad .calBig {
  flex: 1; min-height: 120px; border-radius: 24px; background: var(--card2);
  border: 2px solid var(--line); font-size: 36px; font-weight: 800; color: var(--ink);
}
#calPad .calMid { display: flex; flex-direction: column; gap: 10px; }
#calPad .calSmall {
  min-width: 90px; min-height: 48px; border-radius: 24px; font-size: 14px;
  font-weight: 700; background: var(--card); border: 1px solid var(--line); color: #c9d2e6;
}
#calPad button.down { background: var(--accent); border-color: var(--accent); color: #fff; }
#calPad button.given { border-color: #78c850; }
`;

/** How long each edge has to be held still. */
export const SWEEP_HOLD_MS = 800;

/** One orientation event, all three angles. Alpha can be missing on a phone
 * with no compass; the sweep needs it, the controller does not. */
export interface Reading {
  alpha: number | null;
  beta: number;
  gamma: number;
}

export interface CalibrateApi {
  start(): Promise<PostResult>;
  profiles(): Promise<{ id: string; name: string }[]>;
  save(id: string, body: unknown): Promise<PostResult>;
}

export interface CalibrateDeps {
  api: CalibrateApi;
  link: Pick<CalibrateLink, "open" | "close" | "setScene" | "setTel" | "done">;
  /** This phone's id, and a way to take on a saved one instead. */
  phoneId: () => string;
  adoptId: (id: string) => void;
  /** The profile in use now, to start the name from. */
  current: () => Profile | null;
  /** A profile was saved: apply it everywhere. */
  onSaved: (p: Profile) => void;
  tiltAvailable: boolean;
  requestTilt?: () => Promise<boolean>;
  listenTilt: (fn: (r: Reading) => void) => () => void;
  /** screen.orientation.angle. */
  angle: () => number;
  now: () => number;
  /** Close the sheet. */
  close: () => void;
}

export type Step = "intro" | "starting" | "sweep" | "targets" | "tilt" | "buttons" | "summary";

const EDGE_WORDS: Record<Edge, string> = {
  left: "LEFT edge",
  right: "RIGHT edge",
  top: "TOP edge",
  bottom: "BOTTOM edge",
};
const DIR_WORDS: Record<Direction, string> = {
  left: "left",
  right: "right",
  up: "away from you",
  down: "towards you",
};
const DIR_EDGE: Record<Direction, Edge> = { left: "left", right: "right", up: "top", down: "bottom" };

export class CalibrateSheet {
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly body: HTMLElement;
  private readonly live: HTMLElement;
  private readonly noteEl: HTMLElement;

  step: Step = "intro";
  name = "";
  private saved: { id: string; name: string }[] = [];
  private label = "";
  private reading: Reading | null = null;
  private touch: [number, number] | null = null;
  private unlisten: (() => void) | null = null;

  // sweep
  private edge = 0;
  private holds: Partial<Record<Edge, Hold>> = {};
  private holding: { since: number; samples: Orientation[] } | null = null;
  fit: PointerFit | null = null;

  // targets
  private target = 0;
  private errors: number[] = [];
  private aiming: { since: number; misses: number[] } | null = null;
  dot: Point | null = null;

  // tilt
  readonly pad = new TiltPad();
  tilt: TiltCalibration = TILT_CALIBRATION_DEFAULT;
  private dir = -1;
  private capture: TiltCapture | null = null;

  // buttons
  private picker = new ButtonPicker();
  buttons: ButtonMap = { ...BUTTONS_DEFAULT };
  private pressed = 0;
  /** The pad's buttons and the prompt above them, updated in place while a
   * finger is on one: a repaint would replace the element under the finger,
   * and its pointerup would go to an element no longer in the page. */
  private padButtons: Partial<Record<ButtonName, HTMLElement>> = {};
  private prompt: HTMLElement | null = null;
  private repaintOnRelease = false;

  // summary
  profile: Profile | null = null;
  private stored = false;
  private saving = false;

  constructor(doc: Document, private readonly deps: CalibrateDeps) {
    this.doc = doc;
    this.body = h(doc, "div", { id: "calBody" });
    this.live = h(doc, "div", { id: "calLive" });
    this.noteEl = h(doc, "div", { id: "calNote" });
    this.root = h(
      doc,
      "div",
      { id: "calibrateSheet", class: "sheet", role: "dialog", modal: true, aria: "Calibrate phone" },
      [grip(doc), head(doc, "Calibrate phone"), this.body, this.live, this.noteEl],
    );
    const at = (ev: Event): [number, number] | null => {
      const e = ev as { clientX?: number; clientY?: number };
      const view = doc.defaultView;
      const w = view?.innerWidth || 1;
      const hh = view?.innerHeight || 1;
      if (e.clientX === undefined || e.clientY === undefined) {
        return null;
      }
      return [Math.min(1, Math.max(0, e.clientX / w)), Math.min(1, Math.max(0, e.clientY / hh))];
    };
    this.root.addEventListener("pointerdown", (ev) => this.onTouch(at(ev)));
    this.root.addEventListener("pointermove", (ev) => {
      if (this.touch) {
        this.onTouch(at(ev));
      }
    });
    for (const type of ["pointerup", "pointercancel"]) {
      this.root.addEventListener(type, () => this.onTouch(null));
    }
  }

  // --- from the page ---------------------------------------------------------

  /** The sheet opened: fetch the saved profiles for the intro. */
  opened(): void {
    if (this.step === "intro") {
      this.name = this.deps.current()?.name || this.name;
      void this.deps.api.profiles().then(
        (list) => {
          this.saved = list;
          if (this.step === "intro") {
            this.paint();
          }
        },
        () => undefined,
      );
    }
    this.paint();
  }

  /** The sheet went away, however it went: the black screen goes too. */
  closed(): void {
    if (this.step !== "intro") {
      this.stop();
    }
  }

  onStatus(s: Status): void {
    this.label = s.prepare?.label ?? "";
    if (this.step === "starting") {
      if (s.state === "calibrating") {
        this.toSweep();
      } else if (s.state !== "preparing" && s.notice) {
        this.stop(s.notice);
      } else {
        this.paint();
      }
      return;
    }
    if (this.step !== "intro" && s.state !== "calibrating") {
      this.stop("The calibration screen was closed on the projector.");
    }
  }

  // --- the readings -------------------------------------------------------

  /** One orientation event. Everything the steps measure comes through here. */
  onReading(r: Reading): void {
    this.reading = r;
    const now = this.deps.now();
    switch (this.step) {
      case "sweep":
        this.sweepReading(r, now);
        break;
      case "targets":
        this.targetReading(r, now);
        break;
      case "tilt":
        this.tiltReading(r);
        break;
      default:
        break;
    }
    this.sendTel();
    this.paintLive();
  }

  private onTouch(p: [number, number] | null): void {
    this.touch = p;
    this.sendTel();
  }

  private sendTel(): void {
    const r = this.reading;
    const tel: Telemetry = {
      a: r?.alpha ?? null,
      b: r?.beta ?? null,
      g: r?.gamma ?? null,
      angle: Math.round(this.deps.angle() || 0),
      touch: this.touch,
      mask: this.lights(),
      dot: this.step === "targets" && this.dot ? [this.dot.x, this.dot.y] : null,
    };
    this.deps.link.setTel(tel);
  }

  private lights(): number {
    if (this.step === "tilt") {
      return this.pad.held;
    }
    if (this.step === "buttons") {
      return this.pressed;
    }
    return 0;
  }

  // --- starting ------------------------------------------------------------

  /** Start, from the tap that iOS wants before it hands over the tilt. */
  async begin(): Promise<void> {
    const input = this.body.querySelector?.("#calName") as HTMLInputElement | null;
    const typed = (input?.value ?? this.name).trim();
    this.name = typed || "My phone";
    if (!this.deps.tiltAvailable) {
      this.note("Calibration needs the page opened over https://.");
      return;
    }
    if (this.deps.requestTilt) {
      const ok = await this.deps.requestTilt();
      if (!ok) {
        this.note("Tilting was not allowed, so the phone cannot be calibrated.");
        return;
      }
    }
    this.listen();
    this.deps.link.open();
    this.step = "starting";
    this.note("");
    this.scene();
    this.paint();
    const r = await this.deps.api.start();
    if (!r.ok) {
      this.stop(r.data.error || "The projector would not start.");
      return;
    }
    if (r.data["state"] === "calibrating") {
      this.toSweep();
    }
  }

  /** Stop, from any step: the black screen goes and the sheet starts over. */
  stop(why = ""): void {
    this.deps.link.done();
    this.deps.link.close();
    if (this.unlisten) {
      this.unlisten();
      this.unlisten = null;
    }
    this.reset();
    this.note(why);
    this.paint();
  }

  private reset(): void {
    this.step = "intro";
    this.edge = 0;
    this.holds = {};
    this.holding = null;
    this.fit = null;
    this.target = 0;
    this.errors = [];
    this.aiming = null;
    this.dot = null;
    this.tilt = TILT_CALIBRATION_DEFAULT;
    this.pad.cal = this.tilt;
    this.pad.reset();
    this.dir = -1;
    this.capture = null;
    this.picker = new ButtonPicker();
    this.buttons = { ...BUTTONS_DEFAULT };
    this.pressed = 0;
    this.profile = null;
    this.stored = false;
    this.saving = false;
  }

  private listen(): void {
    if (!this.unlisten) {
      this.unlisten = this.deps.listenTilt((r) => this.onReading(r));
    }
  }

  // --- the sweep -----------------------------------------------------------

  toSweep(): void {
    this.step = "sweep";
    this.edge = 0;
    this.holds = {};
    this.holding = null;
    this.scene();
    this.paint();
  }

  /** The hold button went down. */
  holdStart(): void {
    if (this.step === "sweep") {
      this.holding = { since: this.deps.now(), samples: [] };
      this.scene();
    }
  }

  /** ...and came up before the hold was done: that one does not count. */
  holdEnd(): void {
    if (this.holding) {
      this.holding = null;
      this.scene();
    }
  }

  private sweepReading(r: Reading, now: number): void {
    const held = this.holding;
    if (!held) {
      return;
    }
    if (r.alpha === null) {
      this.note("This phone isn't saying which way it points, so the pointer can't be calibrated.");
      return;
    }
    held.samples.push({ alpha: r.alpha, beta: r.beta });
    if (now - held.since < SWEEP_HOLD_MS) {
      this.scene();
      return;
    }
    const hold = summarise(held.samples);
    this.holding = null;
    if (!hold) {
      return;
    }
    this.holds[EDGES[this.edge]!] = hold;
    this.edge++;
    if (this.edge < EDGES.length) {
      this.scene();
      this.paint();
      return;
    }
    const fit = fitPointer(this.holds as Record<Edge, Hold>);
    if ("error" in fit) {
      this.note(fit.error);
      this.edge = 0;
      this.holds = {};
      this.scene();
      this.paint();
      return;
    }
    this.fit = fit;
    this.note("");
    this.toTargets();
  }

  // --- the hit zones -------------------------------------------------------

  toTargets(): void {
    this.step = "targets";
    this.target = 0;
    this.errors = [];
    this.aiming = null;
    this.dot = null;
    this.scene();
    this.paint();
  }

  /** Point at the middle of the picture and tap: the gyro drifts, and this
   * puts the middle back where the phone is pointing now. */
  recentre(): void {
    if (this.fit && this.reading && this.reading.alpha !== null) {
      this.fit = { ...this.fit, centre: { alpha: this.reading.alpha, beta: this.reading.beta } };
      this.dot = null;
    }
  }

  skipTarget(): void {
    if (this.step === "targets" && this.target < TARGETS.length) {
      this.hitTarget(1);
    }
  }

  private targetReading(r: Reading, now: number): void {
    const fit = this.fit;
    if (!fit || r.alpha === null) {
      return;
    }
    const want = aim({ x: 0.5, y: 0.5 }, fit.centre, { alpha: r.alpha, beta: r.beta }, 1, fit.cal);
    const k = fit.cal.smoothing;
    this.dot = this.dot
      ? { x: this.dot.x + (want.x - this.dot.x) * k, y: this.dot.y + (want.y - this.dot.y) * k }
      : want;
    const t = TARGETS[this.target];
    if (!t) {
      return;
    }
    if (!inside(this.dot, t)) {
      if (this.aiming) {
        this.aiming = null;
        this.scene();
      }
      return;
    }
    if (!this.aiming) {
      this.aiming = { since: now, misses: [] };
    }
    this.aiming.misses.push(miss(this.dot, t));
    if (now - this.aiming.since >= TARGET_HOLD_MS) {
      const m = this.aiming.misses;
      this.hitTarget(m.reduce((a, b) => a + b, 0) / m.length);
      return;
    }
    this.scene();
  }

  private hitTarget(error: number): void {
    this.errors.push(Math.round(error * 10000) / 10000);
    this.target++;
    this.aiming = null;
    this.scene();
    this.paint();
  }

  /** The mean miss across the targets that were hit, as a fraction of the width. */
  get meanMiss(): number {
    return this.errors.length ? this.errors.reduce((a, b) => a + b, 0) / this.errors.length : 0;
  }

  // --- the controller -------------------------------------------------------

  toTilt(): void {
    this.step = "tilt";
    this.tilt = TILT_CALIBRATION_DEFAULT;
    this.pad.cal = this.tilt;
    this.pad.reset();
    this.dir = -1;
    this.capture = null;
    this.scene();
    this.paint();
  }

  /** Held like a gamepad, the way they will play: this is level. */
  tiltReady(): void {
    this.pad.reset();
    this.dir = 0;
    this.capture = new TiltCapture(DIRECTIONS[0]!);
    this.scene();
    this.paint();
  }

  private tiltReading(r: Reading): void {
    this.pad.update({ beta: r.beta, gamma: r.gamma }, this.deps.angle());
    const offset = this.pad.offset;
    const capture = this.capture;
    if (!capture || !offset) {
      this.scene();
      return;
    }
    const got = capture.update(offset);
    if (!got) {
      this.scene();
      return;
    }
    const dir = capture.dir;
    if (got.flipped) {
      // The first direction on an axis decides which way round it is. The
      // second one going the wrong way is a mistake, not a preference.
      if (dir === "left" || dir === "up") {
        const key = dir === "left" ? "roll_sign" : "pitch_sign";
        this.setTilt({ [key]: -this.tilt[key] });
        this.note(dir === "left" ? "Swapped left and right to match how you hold it." : "Swapped up and down to match how you hold it.");
      } else {
        this.note("That was the other way. Tilt " + DIR_WORDS[dir] + ".");
        this.capture = new TiltCapture(dir);
        this.scene();
        return;
      }
    }
    this.setTilt({ press: { ...this.tilt.press, [dir]: pressFor(got.peak) } });
    this.dir++;
    const next = DIRECTIONS[this.dir];
    this.capture = next ? new TiltCapture(next) : null;
    if (!next) {
      this.toButtons();
      return;
    }
    this.scene();
    this.paint();
  }

  /** Flip an axis by hand. */
  flip(axis: "roll_sign" | "pitch_sign"): void {
    this.setTilt({ [axis]: -this.tilt[axis] });
    this.paint();
  }

  private setTilt(change: Partial<TiltCalibration>): void {
    this.tilt = { ...this.tilt, ...change };
    this.pad.cal = this.tilt;
  }

  // --- the buttons ---------------------------------------------------------

  toButtons(): void {
    this.step = "buttons";
    this.picker = new ButtonPicker();
    this.buttons = { ...BUTTONS_DEFAULT };
    this.pressed = 0;
    this.scene();
    this.paint();
  }

  /** An on-screen button went down in the button step. */
  press(name: ButtonName): void {
    if (this.step !== "buttons") {
      return;
    }
    if (!this.picker.done) {
      const want = this.picker.wanted!;
      const r = this.picker.tap(name);
      if (r.already) {
        this.note("That one is already " + r.already + ".");
        this.pressed = BUTTONS_DEFAULT[r.already];
      } else {
        this.note("");
        this.pressed = BUTTONS_DEFAULT[want];
        if (this.picker.done) {
          this.buttons = this.picker.map();
          // The buttons to carry on with appear when the finger lifts.
          this.repaintOnRelease = true;
        }
      }
      this.paintPad();
    } else {
      this.pressed = this.buttons[name];
    }
    this.scene();
    this.sendTel();
  }

  release(): void {
    this.pressed = 0;
    this.scene();
    this.sendTel();
    if (this.repaintOnRelease) {
      this.repaintOnRelease = false;
      this.paint();
    }
  }

  swapAB(): void {
    this.buttons = swapAB(this.buttons);
    this.paint();
  }

  // --- the summary ---------------------------------------------------------

  toSummary(): void {
    if (!this.fit) {
      return;
    }
    this.step = "summary";
    this.profile = {
      name: this.name,
      pointer: this.fit.cal,
      tilt: this.tilt,
      buttons: this.buttons,
      checks: { targets: this.errors, buttons_ok: this.picker.done },
    };
    this.stored = false;
    this.scene();
    this.paint();
  }

  async save(): Promise<void> {
    const p = this.profile;
    if (!p || this.saving) {
      return;
    }
    this.saving = true;
    this.paint();
    const r = await this.deps.api.save(this.deps.phoneId(), profileBody(p)).catch(
      (): PostResult => ({ ok: false, data: { error: "The stick did not answer." } }),
    );
    this.saving = false;
    if (!r.ok) {
      this.note(r.data.error || "That didn't save.");
      this.paint();
      return;
    }
    const stored = parseProfile(r.data) ?? p;
    this.profile = stored;
    this.stored = true;
    this.note("");
    this.deps.onSaved(stored);
    this.paint();
  }

  /** The last button: the black screen goes and the sheet closes. */
  finish(): void {
    this.stop();
    this.deps.close();
  }

  // --- what the projector shows -------------------------------------------

  private scene(): void {
    const base = { name: this.name };
    let scene: Scene;
    switch (this.step) {
      case "sweep": {
        const held = this.holding;
        scene = {
          ...base,
          step: "sweep",
          edge: EDGES[this.edge] ?? null,
          hold: held ? Math.min(1, (this.deps.now() - held.since) / SWEEP_HOLD_MS) : 0,
        };
        break;
      }
      case "targets":
        scene = {
          ...base,
          step: "targets",
          hold: this.aiming ? Math.min(1, (this.deps.now() - this.aiming.since) / TARGET_HOLD_MS) : 0,
          targets: TARGETS.map((t, i) => ({
            ...t,
            state: i < this.target ? "hit" : i === this.target ? "active" : "pending",
          })),
        };
        break;
      case "tilt": {
        const dir = DIRECTIONS[this.dir];
        scene = {
          ...base,
          step: "tilt",
          edge: dir ? DIR_EDGE[dir] : null,
          lights: this.pad.held,
          hold: this.capture?.progress ?? 0,
        };
        break;
      }
      case "buttons":
        scene = { ...base, step: "buttons", button: this.picker.wanted, lights: this.pressed };
        break;
      case "summary": {
        const p = this.profile!;
        const press = p.tilt.press;
        scene = {
          ...base,
          step: "summary",
          summary: {
            yaw_across: p.pointer.yaw_across,
            pitch_across: p.pointer.pitch_across,
            error: this.meanMiss * 100,
            press: (press.left + press.right + press.up + press.down) / 4,
            jitter: this.fit?.jitter ?? 0,
          },
        };
        break;
      }
      default:
        scene = { ...base, step: "intro" };
    }
    this.deps.link.setScene(scene);
  }

  // --- the sheet ------------------------------------------------------------

  note(text: string): void {
    setText(this.noteEl, text);
  }

  private paintLive(): void {
    const r = this.reading;
    if (!r) {
      setText(this.live, "");
      return;
    }
    const f = (v: number | null): string => (v === null ? "—" : v.toFixed(1) + "°");
    setText(this.live, "α " + f(r.alpha) + "   β " + f(r.beta) + "   γ " + f(r.gamma));
  }

  paint(): void {
    const doc = this.doc;
    const b = this.body;
    b.textContent = "";
    const p = (text: string, cls = ""): HTMLElement => h(doc, "p", { class: cls, text });
    const go = (text: string, fn: () => void, id?: string): HTMLElement =>
      h(doc, "button", { type: "button", class: "calGo", text, onTap: fn, ...(id ? { id } : {}) });
    const alt = (text: string, fn: () => void): HTMLElement =>
      h(doc, "button", { type: "button", class: "calAlt", text, onTap: fn });
    const stop = (): HTMLElement => alt("Stop calibrating", () => this.finish());

    switch (this.step) {
      case "intro": {
        b.appendChild(
          p(
            "This sets up the pointer and the gamepad for this phone. It shows on the projector, so sit where you will play from.",
            "lead",
          ),
        );
        const input = h(doc, "input", { id: "calName", aria: "Name for this phone" }) as HTMLInputElement;
        input.type = "text";
        input.placeholder = "Whose phone is this?";
        input.maxLength = 32;
        input.value = this.name;
        input.addEventListener("input", () => {
          this.name = input.value;
        });
        b.appendChild(input);
        const mine = this.deps.phoneId();
        for (const s of this.saved) {
          const pick = h(doc, "button", {
            type: "button",
            class: s.id === mine ? "row sel" : "row",
            text: s.id === mine ? s.name + " (this phone)" : "This is " + s.name,
            onTap: () => {
              this.deps.adoptId(s.id);
              this.name = s.name;
              this.paint();
            },
          });
          b.appendChild(pick);
        }
        if (!this.deps.tiltAvailable) {
          b.appendChild(p("Calibration needs the page opened over https://."));
        }
        const start = go("Start on the projector", () => void this.begin(), "calStart");
        (start as HTMLButtonElement).disabled = !this.deps.tiltAvailable;
        b.appendChild(start);
        break;
      }
      case "starting":
        b.appendChild(p("Getting the projector ready…", "lead"));
        b.appendChild(p(this.label || "This can take a minute while the lamp warms up."));
        b.appendChild(stop());
        break;
      case "sweep": {
        const edge = EDGES[this.edge]!;
        b.appendChild(
          p(
            "Point the top of the phone at the " +
              EDGE_WORDS[edge] +
              " of the picture. Then press and hold the button until the bar on the projector fills.",
            "lead",
          ),
        );
        const hold = h(doc, "button", {
          type: "button",
          class: "calHold",
          id: "calHold",
          text: "Hold: " + EDGE_WORDS[edge] + " (" + (this.edge + 1) + " of 4)",
        });
        hold.addEventListener("pointerdown", (ev) => {
          (ev as { preventDefault?: () => void }).preventDefault?.();
          hold.classList.add("held");
          this.holdStart();
        });
        for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
          hold.addEventListener(type, () => {
            hold.classList.remove("held");
            this.holdEnd();
          });
        }
        b.appendChild(hold);
        b.appendChild(stop());
        break;
      }
      case "targets": {
        if (this.target < TARGETS.length) {
          b.appendChild(
            p(
              "Aim the red dot at the yellow circle on the projector and keep it there. Target " +
                (this.target + 1) +
                " of " +
                TARGETS.length +
                ".",
              "lead",
            ),
          );
          b.appendChild(p("If the dot is off, point at the middle of the picture and tap Re-centre."));
          b.appendChild(go("Re-centre", () => this.recentre()));
          b.appendChild(alt("Skip this target", () => this.skipTarget()));
        } else {
          const pct = (this.meanMiss * 100).toFixed(1);
          b.appendChild(p("All five done. On average the dot was " + pct + "% of the width off.", "lead"));
          if (this.meanMiss > TARGET_REDO) {
            b.appendChild(p("That is quite far. Doing the edges again usually helps."));
          }
          b.appendChild(go("Next: the gamepad", () => this.toTilt()));
        }
        b.appendChild(alt("Do the edges again", () => this.toSweep()));
        b.appendChild(stop());
        break;
      }
      case "tilt": {
        const dir = DIRECTIONS[this.dir];
        if (!dir) {
          b.appendChild(
            p("Hold the phone sideways in both hands, like a gamepad, the way you will play. Then tap Ready.", "lead"),
          );
          b.appendChild(go("Ready", () => this.tiltReady()));
        } else {
          b.appendChild(
            p("Tilt the phone " + DIR_WORDS[dir] + " as far as is comfortable, then back to the middle.", "lead"),
          );
          b.appendChild(alt("Start the tilting again", () => this.toTilt()));
        }
        b.appendChild(alt("Swap left and right", () => this.flip("roll_sign")));
        b.appendChild(alt("Swap up and down", () => this.flip("pitch_sign")));
        b.appendChild(stop());
        break;
      }
      case "buttons": {
        this.prompt = p("", "lead");
        b.appendChild(this.prompt);
        b.appendChild(this.padEl());
        this.paintPad();
        if (this.picker.done) {
          b.appendChild(go("Next: check and save", () => this.toSummary()));
          b.appendChild(alt("Swap A and B", () => this.swapAB()));
        }
        b.appendChild(alt("Start the buttons again", () => this.toButtons()));
        b.appendChild(stop());
        break;
      }
      case "summary": {
        const pr = this.profile!;
        const press = pr.tilt.press;
        b.appendChild(p(this.stored ? "Saved. This phone uses it from now on." : "Here is what was measured.", "lead"));
        b.appendChild(
          p(
            "Pointer: " +
              pr.pointer.yaw_across.toFixed(0) +
              "° across, " +
              pr.pointer.pitch_across.toFixed(0) +
              "° down, smoothing " +
              pr.pointer.smoothing.toFixed(2) +
              ". Average miss " +
              (this.meanMiss * 100).toFixed(1) +
              "%.",
          ),
        );
        b.appendChild(
          p(
            "Gamepad tilt: left " +
              press.left +
              "°, right " +
              press.right +
              "°, away " +
              press.up +
              "°, towards " +
              press.down +
              "°.",
          ),
        );
        b.appendChild(
          p(
            "Buttons: left big " +
              bitName(pr.buttons.B) +
              ", right big " +
              bitName(pr.buttons.A) +
              ", SELECT " +
              bitName(pr.buttons.SELECT) +
              ", START " +
              bitName(pr.buttons.START) +
              ".",
          ),
        );
        if (this.stored) {
          b.appendChild(go("Finish", () => this.finish(), "calFinish"));
        } else {
          const save = go(this.saving ? "Saving…" : "Save as " + pr.name, () => void this.save(), "calSave");
          (save as HTMLButtonElement).disabled = this.saving;
          b.appendChild(save);
          b.appendChild(alt("Do the buttons again", () => this.toButtons()));
          b.appendChild(stop());
        }
        break;
      }
    }
  }

  /** The prompt and each pad button's label, without rebuilding them. */
  private paintPad(): void {
    const want = this.picker.wanted;
    if (this.prompt) {
      setText(
        this.prompt,
        want
          ? "Tap the button you want to be " + want + ". It lights up on the projector."
          : "Done. Press any button to see it light up on the projector.",
      );
    }
    for (const [name, el] of Object.entries(this.padButtons) as [ButtonName, HTMLElement][]) {
      const job = this.picker.done ? bitName(this.buttons[name]) : this.picker.given[name];
      setText(el, job ?? "?");
      el.classList.toggle("given", !!job);
    }
  }

  /** The four buttons, laid out like the gamepad: B left, A right, the small
   * two between. Each wears the job it has been given so far. */
  private padEl(): HTMLElement {
    const doc = this.doc;
    this.padButtons = {};
    const button = (name: ButtonName, cls: string): HTMLElement => {
      const el = h(doc, "button", { type: "button", class: cls, aria: name });
      this.padButtons[name] = el;
      el.addEventListener("pointerdown", (ev) => {
        (ev as { preventDefault?: () => void }).preventDefault?.();
        el.classList.add("down");
        this.press(name);
      });
      for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
        el.addEventListener(type, () => {
          el.classList.remove("down");
          if (this.pressed) {
            this.release();
          }
        });
      }
      return el;
    };
    return h(doc, "div", { id: "calPad" }, [
      button("B", "calBig"),
      h(doc, "div", { class: "calMid" }, [button("SELECT", "calSmall"), button("START", "calSmall")]),
      button("A", "calBig"),
    ]);
  }
}
