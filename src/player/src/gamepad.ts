// The gamepad: what a phone shows while a game is on the projector.
//
// Held sideways in both hands. B is under the left thumb and A under the
// right, as they are on the consoles these games are from; tilting the phone
// is the d-pad (tilt-pad.ts). Start and Select are small and in the middle,
// where a thumb does not land by accident. STOP is a long press, for the same
// reason the logo's menu is: a game ended by a stray tap is a child in tears.
//
// Every phone that is open while a game runs shows this view. The first one
// to open it after the game starts is player one; the rest listen -- they hear
// the game -- and have a button to join as player two when the game has a
// second pad. Which seat a phone has is the daemon's to say, over the socket
// (game-link.ts), so this view only draws what it is told.
//
// A phone that has been through the calibration wizard may have its buttons
// rearranged: which of the four on-screen buttons sends A, B, START and SELECT
// is its ButtonMap, and each button wears the name of what it sends. With no
// profile, BUTTONS_DEFAULT is the layout above.
import { h, setText, toggleClass } from "./dom";
import { holdToOpen } from "./secret-menu";
import type { GameAudio } from "./game-audio";
import type { GameInfo, GameLink, Role } from "./game-link";
import {
  A,
  B,
  SELECT,
  START,
  TILT_CALIBRATION_DEFAULT,
  TiltPad,
  type Tilt,
  type TiltCalibration,
} from "./tilt-pad";
import type { Status } from "./types";

export const GAMEPAD_CSS = `
#gamepad { user-select: none; -webkit-user-select: none; touch-action: none; }
#gamepad.on { display: flex; }
#gamepad {
  flex-direction: column; min-height: 100vh; min-height: 100dvh;
  padding: 10px 0 14px;
}
#padTop { display: flex; align-items: center; gap: 10px; margin: 0 4px 8px; }
#padTitle { flex: 1; min-width: 0; font-size: 20px; font-weight: 700;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#padRole { color: var(--dim); font-size: 15px; white-space: nowrap; }
#padTop button {
  min-height: 44px; padding: 0 14px; border-radius: 12px; font-size: 15px;
  font-weight: 650; background: var(--card); border: 1px solid var(--line);
  color: var(--ink);
}
#padTop button[hidden] { display: none; }
#padJoin { background: var(--accent) !important; border-color: var(--accent) !important; }
#padStop { color: #ff8fa3 !important; }
#padBody { flex: 1; display: flex; align-items: center; gap: 14px; }
.padBig {
  flex: 1; align-self: stretch; min-height: 160px; border-radius: 28px;
  background: var(--card2); border: 2px solid var(--line);
  font-size: 44px; font-weight: 800; color: var(--ink);
}
.padBig.down { background: var(--accent); border-color: var(--accent); transform: scale(.97); }
#padMid { display: flex; flex-direction: column; gap: 10px; align-items: center; }
#padMid button {
  min-width: 96px; min-height: 48px; border-radius: 24px; font-size: 14px;
  font-weight: 700; letter-spacing: .5px; background: var(--card);
  border: 1px solid var(--line); color: #c9d2e6;
}
#padMid button.down { background: var(--accent); color: #fff; }
#padMid button[hidden] { display: none; }
#padTilt { color: var(--dim); font-size: 14px; text-align: center; min-height: 18px; }
#padTurn { display: none; color: var(--dim); font-size: 18px; text-align: center;
  padding: 30px 12px; }
@media (orientation: portrait) {
  #gamepad.on #padTurn { display: block; }
}
`;

/** The four on-screen buttons, named by where they are in the default layout:
 * A is the big one on the right, B the big one on the left. */
export type ButtonName = "A" | "B" | "START" | "SELECT";
export const BUTTON_NAMES: readonly ButtonName[] = ["A", "B", "START", "SELECT"];
/** Which mask bit each on-screen button sends. */
export type ButtonMap = Record<ButtonName, number>;
export const BUTTONS_DEFAULT: ButtonMap = { A, B, START, SELECT };
/** The name a mask bit goes by, for the label on whichever button sends it. */
export function bitName(bit: number): ButtonName {
  return BUTTON_NAMES.find((n) => BUTTONS_DEFAULT[n] === bit) ?? "A";
}

export interface GamepadDeps {
  link: GameLink;
  audio: GameAudio;
  /** iOS asks before a page may read the tilt, and only from a tap. */
  requestTilt?: () => Promise<boolean>;
  /** Whether this browser gives a page orientation at all. */
  tiltAvailable: boolean;
  listenTilt: (fn: (t: Tilt) => void) => () => void;
  /** screen.orientation.angle, or window.orientation on an older Safari. */
  angle: () => number;
  stop: () => void;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  vibrate?: (ms: number) => void;
}

export class GamepadView {
  readonly root: HTMLElement;
  readonly tilt = new TiltPad();
  private readonly title: HTMLElement;
  private readonly roleEl: HTMLElement;
  private readonly joinBtn: HTMLElement;
  private readonly soundBtn: HTMLElement;
  private readonly tiltBtn: HTMLElement;
  private readonly tiltLine: HTMLElement;
  private readonly selectBtn: HTMLElement;
  private buttons = 0;
  private map: ButtonMap = { ...BUTTONS_DEFAULT };
  private readonly els = {} as Record<ButtonName, HTMLElement>;
  private unlisten: (() => void) | null = null;
  private active = false;
  private role: Role = "";
  private game: GameInfo | null = null;
  private rate = 0;

  constructor(doc: Document, private readonly deps: GamepadDeps) {
    this.title = h(doc, "div", { id: "padTitle" });
    this.roleEl = h(doc, "div", { id: "padRole" });
    this.joinBtn = h(doc, "button", {
      id: "padJoin",
      type: "button",
      text: "Join as player 2",
      hidden: true,
      onTap: () => {
        this.unlock();
        deps.link.join("any");
      },
    });
    this.soundBtn = h(doc, "button", {
      id: "padSound",
      type: "button",
      text: "Sound on",
      onTap: () => this.unlock(),
    });
    const stop = h(doc, "button", {
      id: "padStop",
      type: "button",
      text: "Hold to stop",
      aria: "Hold to stop the game",
    });
    holdToOpen(stop, () => deps.stop(), {
      schedule: deps.schedule,
      cancel: deps.cancel,
    });

    const bBtn = this.pad(doc, "padB", "B", "padBig");
    const aBtn = this.pad(doc, "padA", "A", "padBig");
    this.selectBtn = this.pad(doc, "padSelect", "SELECT");
    const startBtn = this.pad(doc, "padStart", "START");
    this.tiltBtn = h(doc, "button", {
      id: "padCenter",
      type: "button",
      text: "Centre",
      aria: "Treat how you are holding the phone now as level",
      onTap: () => this.recenter(),
    });
    this.tiltLine = h(doc, "div", { id: "padTilt" });

    this.root = h(doc, "div", { id: "gamepad", class: "view" }, [
      h(doc, "div", { id: "padTop" }, [
        this.title,
        this.roleEl,
        this.joinBtn,
        this.soundBtn,
        stop,
      ]),
      h(doc, "div", { id: "padTurn", text: "Turn your phone sideways to play." }),
      h(doc, "div", { id: "padBody" }, [
        bBtn,
        h(doc, "div", { id: "padMid" }, [this.selectBtn, startBtn, this.tiltBtn, this.tiltLine]),
        aBtn,
      ]),
    ]);
  }

  /** Apply this phone's calibration: the tilt's press points and signs, and
   * which on-screen button sends what. Null puts the defaults back. */
  setCalibration(tilt: TiltCalibration | null, buttons: ButtonMap | null): void {
    this.tilt.cal = tilt ?? TILT_CALIBRATION_DEFAULT;
    this.map = { ...(buttons ?? BUTTONS_DEFAULT) };
    for (const name of BUTTON_NAMES) {
      setText(this.els[name], bitName(this.map[name]));
    }
  }

  /** Which mask bit the on-screen button `name` sends. */
  bitFor(name: ButtonName): number {
    return this.map[name];
  }

  /** The mask the phone is holding now: thumbs, and the tilt when it is on. */
  get mask(): number {
    return this.buttons | (this.unlisten ? this.tilt.held : 0);
  }

  /** From the tap that started the game, before the view is even up: the one
   * moment iOS allows both sound and the tilt to be switched on. */
  prime(): void {
    this.unlock();
    this.enableTilt();
  }

  /** A poll said a game is on. */
  apply(s: Status): void {
    if (s.title) {
      setText(this.title, s.title);
    }
    if (!this.active) {
      this.active = true;
      this.deps.link.open();
      this.enableTilt();
    }
    const rate = s.game?.rate ?? 0;
    if (rate && rate !== this.rate && this.deps.audio.on) {
      this.rate = rate;
      this.deps.audio.start(rate);
    }
    this.rate = rate || this.rate;
    this.paint();
  }

  /** The game ended, or this phone left the view. */
  leave(): void {
    if (!this.active) {
      return;
    }
    this.active = false;
    this.deps.link.close();
    this.deps.audio.stop();
    this.disableTilt();
    this.buttons = 0;
    this.role = "";
    this.game = null;
    this.rate = 0;
  }

  onRole(role: Role, game: GameInfo | null): void {
    this.role = role;
    this.game = game;
    if (game?.rate) {
      this.rate = game.rate;
    }
    this.paint();
  }

  onTilt(t: Tilt): void {
    this.tilt.update(t, this.deps.angle());
    this.send();
  }

  /** Level from now on -- and, the first time, the tap that switches the
   * tilt on where the browser wants one. */
  private recenter(): void {
    if (!this.unlisten) {
      this.enableTilt();
      return;
    }
    this.tilt.recenter();
    this.send();
  }

  private unlock(): void {
    this.deps.audio.unlock();
    if (this.rate) {
      this.deps.audio.start(this.rate);
    }
    this.paint();
  }

  private enableTilt(): void {
    if (this.unlisten || !this.deps.tiltAvailable) {
      this.paint();
      return;
    }
    const listen = (): void => {
      if (this.unlisten) {
        return;
      }
      this.tilt.reset();
      this.unlisten = this.deps.listenTilt((t) => this.onTilt(t));
      this.paint();
    };
    if (this.deps.requestTilt) {
      void this.deps.requestTilt().then((ok) => {
        if (ok && this.active) {
          listen();
        }
        this.paint();
      });
    } else {
      listen();
    }
  }

  private disableTilt(): void {
    if (this.unlisten) {
      this.unlisten();
      this.unlisten = null;
    }
    this.tilt.reset();
  }

  private send(): void {
    this.deps.link.setMask(this.mask);
  }

  /** One thumb button: held while touched, let go on lift or slide-off.
   * The bit is looked up when it is pressed and remembered until it is let
   * go, so a profile arriving under a thumb cannot leave a button stuck. */
  private pad(doc: Document, id: string, name: ButtonName, cls = ""): HTMLElement {
    const el = h(doc, "button", { id, type: "button", class: cls, text: name });
    this.els[name] = el;
    let held = 0;
    const down = (ev: Event): void => {
      (ev as { preventDefault?: () => void }).preventDefault?.();
      if (!this.deps.audio.on) {
        this.unlock();
      }
      held = this.map[name];
      this.buttons |= held;
      toggleClass(el, "down", true);
      this.deps.vibrate?.(8);
      this.send();
    };
    const up = (): void => {
      if (held && this.buttons & held) {
        this.buttons &= ~held;
        held = 0;
        toggleClass(el, "down", false);
        this.send();
      }
    };
    el.addEventListener("pointerdown", down);
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
      el.addEventListener(type, up);
    }
    el.addEventListener("contextmenu", (ev) => {
      (ev as { preventDefault?: () => void }).preventDefault?.();
    });
    return el;
  }

  private paint(): void {
    const playing = this.role === "p1" || this.role === "p2";
    setText(
      this.roleEl,
      this.role === "p1"
        ? "Player 1"
        : this.role === "p2"
          ? "Player 2"
          : this.role === "listener"
            ? "Listening"
            : "Connecting…",
    );
    const seats = this.game?.seats ?? [];
    const players = this.game?.players ?? 1;
    this.joinBtn.hidden = !(this.role === "listener" && players > 1 && seats[1] === false);
    this.soundBtn.hidden = this.deps.audio.on;
    toggleClass(this.root, "listening", !playing);
    this.selectBtn.hidden = this.game?.system === "sms";
    setText(
      this.tiltLine,
      !this.deps.tiltAvailable
        ? "Tilt needs https"
        : this.unlisten
          ? "Tilt to move"
          : "Tap Centre to use tilt",
    );
    this.tiltBtn.hidden = false;
  }
}
