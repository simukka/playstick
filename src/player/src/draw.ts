// Drawing on the film: the sheet the now-playing view's Draw button opens.
//
// It works the way the pointer does (pointer.ts), and borrows its arithmetic:
// a touchpad that moves a cursor on the screen like a laptop's, and, where the
// browser hands over the phone's orientation, tilting while a button is held.
// The cursor is a ring on the film the size of what it will draw. Pen down
// ("Draw"), wherever the cursor goes it leaves a line; pen up ("Move"), it
// only moves. The phone shows no picture -- everybody looks at the screen,
// which is the point of drawing on it.
//
// How far a drag or a turn moves the cursor is the pointer's: its speed
// setting, its tilt setting, and this phone's calibration if it has one.
//
// Every phone with this sheet open draws on the same canvas, which the daemon
// keeps (src/server/playstick/draw.py), each with a cursor of its own.
//
// SENDING. One request in flight, at most one per TICK_MS, as the pointer
// does. Unlike the pointer, ink is never dropped: the points that pile up
// while a request is out go in the next one, because a line with holes in it
// is not the line that was drawn. A cursor moved with the pen up is the
// pointer's case exactly, and only the newest position is sent.
import { h, head, setText } from "./dom";
import type { PostResult } from "./net";
import {
  type Orientation,
  type Point,
  type PointerCalibration,
  SMOOTHING,
  aim,
  padMove,
} from "./pointer";
import {
  DRAW_COLORS,
  type DrawColor,
  type DrawPrefs,
  type DrawSize,
  type DrawTool,
  type StorageLike,
  loadDrawPrefs,
  loadPointerPrefs,
  saveDrawPrefs,
} from "./prefs";
import type { Status } from "./types";

export const DRAW_CSS = `
#drawBtn {
  display: flex; align-items: center; justify-content: center; gap: 10px;
  width: 100%; margin: 14px auto 0; max-width: 420px; min-height: 64px;
  border-radius: 18px; background: var(--card); border: 1px solid var(--line);
  color: var(--ink); font-size: 20px; font-weight: 650;
  transition: transform .08s ease;
}
#drawBtn svg { width: 26px; height: 26px; display: block; }
#drawBtn:active { transform: scale(.97); }
#drawPad {
  height: 36vh; border-radius: 18px; background: var(--card);
  border: 1px solid var(--line); touch-action: none;
  display: flex; align-items: center; justify-content: center; text-align: center;
  color: var(--dim); font-size: 16px; padding: 18px;
  -webkit-user-select: none; user-select: none;
}
#drawPad.live { border-color: var(--accent); }
#drawAim {
  display: none; width: 100%; min-height: 60px; margin-top: 12px;
  border-radius: 14px; font-size: 18px; font-weight: 650;
  background: var(--accent); color: #fff; touch-action: none;
  -webkit-user-select: none; user-select: none;
}
#drawAim.on { display: block; }
#drawAim.held { filter: brightness(1.2); }
#drawNote { color: var(--dim); font-size: 15px; padding: 8px 4px 0; min-height: 1.4em; }
#drawControls { margin-top: 10px; }
.drawTools button .ico, .drawPen button .ico { display: block; font-size: 22px; line-height: 1.1; }
.swatches { display: flex; flex-wrap: wrap; gap: 10px; padding: 2px 4px 12px; }
.swatches button {
  width: 42px; height: 42px; border-radius: 50%; border: 2px solid var(--line);
}
.swatches button.sel { border-color: #fff; box-shadow: 0 0 0 3px var(--accent); }
#drawClear {
  display: block; width: 100%; min-height: 52px; margin-top: 6px; border-radius: 14px;
  background: var(--card); border: 1px solid var(--line); color: var(--ink);
  font-size: 17px; font-weight: 650;
}
`;

/** A pencil, for the Draw button. */
export const DRAW_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M4 20l1-4L16 5l3 3L8 19z"/><path d="M14 7l3 3"/>' +
  '<path d="M4 20l4-1"/></svg>';

/** The longest the page goes between requests while something is moving. */
export const TICK_MS = 40;
/** How often the page checks in when nothing is moving: it keeps this phone
 * counted as drawing, and a held-still cursor on the screen. */
export const SYNC_MS = 500;
/** The most points in one request; the daemon refuses more. */
export const MAX_BATCH = 400;

/** draw.py's COLORS, as CSS, for the swatches. */
export const COLOR_CSS: Record<DrawColor, string> = {
  red: "#ff303b",
  orange: "#ff9f0a",
  yellow: "#ffd60a",
  green: "#34c759",
  blue: "#0a7aff",
  purple: "#af52de",
  pink: "#ff478a",
  brown: "#8c6140",
  white: "#ffffff",
  black: "#000000",
};

/** Where a cursor is and what it would draw. */
export interface Cursor {
  x: number;
  y: number;
  tool: DrawTool;
  color: DrawColor;
  size: DrawSize;
}

/** The daemon's answer to every drawing request. */
export interface DrawReply {
  shown: boolean;
  painters: number;
  full: boolean;
  error?: string;
}

interface Chunk {
  op: "stroke";
  id: number;
  tool: DrawTool;
  color: DrawColor;
  size: DrawSize;
  points: number[];
  end: boolean;
}
type Msg = Chunk | { op: "clear" } | { op: "leave" };

export interface DrawSenderDeps {
  post: (body: Record<string, unknown>) => Promise<PostResult>;
  now: () => number;
  schedule: (fn: () => void, ms: number) => unknown;
  phone: () => string;
  /** The cursor while a gesture is going on, re-sent when nothing else is so
   * it does not time out under a finger held still. */
  cursor?: () => Cursor | null;
  onReply: (r: DrawReply) => void;
}

/** Ink in order with nothing dropped; the cursor newest-only. */
export class DrawSender {
  private queue: Msg[] = [];
  private move: Cursor | null = null;
  private inFlight = false;
  /** When the pending wake-up is due, or null for none. A newer, nearer one
   * replaces it: points must not sit behind a wait for the next check-in. */
  private timerAt: number | null = null;
  private timerGen = 0;
  private last = -Infinity;
  private open = false;
  sent = 0;

  constructor(private readonly deps: DrawSenderDeps) {}

  start(): void {
    this.open = true;
    this.last = -Infinity;
    this.pump();
  }

  /** The sheet closed: finish what is queued, and say this phone has gone. */
  stop(): void {
    this.open = false;
    this.move = null;
    // Queued, so it goes after this phone's last points rather than before
    // them -- which would count it as drawing again for PAINTER_SECONDS.
    this.queue.push({ op: "leave" });
    this.pump();
  }

  points(id: number, c: Omit<Cursor, "x" | "y">, pts: number[], end: boolean): void {
    // Ink carries the cursor with it; a pen-up position from before it is
    // stale.
    this.move = null;
    const tail = this.queue[this.queue.length - 1];
    if (
      tail &&
      tail.op === "stroke" &&
      tail.id === id &&
      !tail.end &&
      tail.points.length + pts.length <= 2 * MAX_BATCH
    ) {
      tail.points.push(...pts);
      tail.end = end;
    } else {
      this.queue.push({ op: "stroke", id, tool: c.tool, color: c.color, size: c.size, points: [...pts], end });
    }
    this.pump();
  }

  /** The cursor, pen up. Only the newest is sent. */
  cursor(c: Cursor): void {
    this.move = c;
    this.pump();
  }

  clear(): void {
    this.queue.push({ op: "clear" });
    this.pump();
  }

  private pump(): void {
    if (this.inFlight) {
      return;
    }
    if (!this.open && this.queue.length === 0) {
      return;
    }
    const now = this.deps.now();
    const busy = this.queue.length > 0 || this.move !== null;
    const wait = busy ? this.last + TICK_MS - now : this.last + SYNC_MS - now;
    if (wait > 0) {
      this.wait(wait);
      return;
    }
    this.timerAt = null;
    this.timerGen++;
    let body: Record<string, unknown>;
    const msg = this.queue.shift();
    if (msg) {
      body = { ...msg };
    } else {
      const c = this.move ?? this.deps.cursor?.() ?? null;
      this.move = null;
      body = c ? { op: "move", ...c } : { op: "sync" };
    }
    body.phone = this.deps.phone();
    this.inFlight = true;
    this.last = now;
    this.sent++;
    const done = (): void => {
      this.inFlight = false;
      this.pump();
    };
    this.deps.post(body).then(
      (r) => {
        const data = r.data as unknown as DrawReply;
        if (r.ok && typeof data.painters === "number") {
          this.deps.onReply(data);
        }
        done();
      },
      done,
    );
  }

  private wait(ms: number): void {
    const at = this.deps.now() + ms;
    if (this.timerAt !== null && this.timerAt <= at) {
      return;
    }
    this.timerAt = at;
    const gen = ++this.timerGen;
    this.deps.schedule(() => {
      if (gen !== this.timerGen) {
        return;
      }
      this.timerAt = null;
      this.pump();
    }, ms);
  }
}

/** The painters line: who else is at it. */
export function paintersLine(n: number): string {
  if (n <= 1) {
    return "Only you are drawing.";
  }
  return n === 2 ? "You and 1 other are drawing." : `You and ${n - 1} others are drawing.`;
}

export interface DrawSheetDeps {
  sender: DrawSender;
  storage: StorageLike | null;
  /** The pointer's: whether orientation reaches this page at all. */
  tiltAvailable: boolean;
  /** iOS: ask once, from a tap. Absent where no asking is needed. */
  requestTilt?: () => Promise<boolean>;
  /** Start listening; returns the function that stops. */
  listenTilt: (fn: (o: Orientation) => void) => () => void;
  /** This phone's pointer calibration, or null for the defaults. Asked each
   * time, so a calibration saved while the sheet is open is used at once. */
  calibration?: () => PointerCalibration | null;
}

export class DrawSheet {
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly pad: HTMLElement;
  private readonly aimBtn: HTMLElement;
  private readonly note: HTMLElement;
  private readonly controls: HTMLElement;
  prefs: DrawPrefs;
  /** Where the cursor is, 0..1 across and down. Kept between gestures, so a
   * line can be picked up where it was left. */
  dot: Point = { x: 0.5, y: 0.5 };
  /** Pen down: moving the cursor draws. */
  pen = true;
  private playing = false;
  private shown = true;
  private full = false;
  private painters = 1;
  private tiltAllowed = false;
  private nextId = 1;
  /** The stroke the cursor is laying, or null with the pen up. */
  private stroke: number | null = null;
  private gesture = false;
  private drag: { x: number; y: number } | null = null;
  private aiming: { from: Point; base: Orientation | null; smooth: Point | null } | null = null;
  private stopTilt: (() => void) | null = null;

  constructor(doc: Document, private readonly deps: DrawSheetDeps) {
    this.doc = doc;
    this.prefs = loadDrawPrefs(deps.storage);
    this.tiltAllowed = deps.tiltAvailable && !deps.requestTilt;
    this.pad = h(doc, "div", { id: "drawPad" });
    this.aimBtn = h(doc, "button", { id: "drawAim", type: "button" });
    this.note = h(doc, "div", { id: "drawNote" });
    this.controls = h(doc, "div", { id: "drawControls" });
    this.root = h(
      doc,
      "div",
      { id: "drawSheet", class: "sheet", role: "dialog", modal: true, aria: "Draw" },
      [head(doc, "Draw on the film"), this.pad, this.aimBtn, this.note, this.controls],
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

  /** The cursor while a finger is on the pad or the aim button. */
  get cursorNow(): Cursor | null {
    return this.gesture ? this.cursorAt() : null;
  }

  opened(): void {
    this.deps.sender.start();
    this.paint();
  }

  closed(): void {
    this.endAim();
    this.finish();
    this.drag = null;
    this.pad.classList.remove("live");
    this.deps.sender.stop();
  }

  onReply(r: DrawReply): void {
    this.shown = r.shown;
    this.painters = Math.max(1, r.painters);
    this.full = r.full;
    this.paintNote();
  }

  paint(): void {
    setText(
      this.pad,
      this.playing
        ? this.pen
          ? "Drag here to draw"
          : "Drag here to move without drawing"
        : "Drawing goes over a film. Start one first.",
    );
    const tilt = this.deps.tiltAvailable && loadPointerPrefs(this.deps.storage).tilt;
    this.aimBtn.classList.toggle("on", tilt && this.playing);
    setText(
      this.aimBtn,
      !this.tiltAllowed ? "Allow tilting" : this.pen ? "Hold and tilt to draw" : "Hold and tilt to move",
    );
    this.paintNote();
    this.paintControls();
  }

  private paintNote(): void {
    let text = paintersLine(this.painters);
    if (this.playing && !this.shown) {
      text = "Nothing is on the screen to draw on.";
    } else if (this.full) {
      text = "The drawing is full. Clear it to carry on.";
    }
    setText(this.note, this.playing ? text : "");
  }

  // --- the cursor ---------------------------------------------------------

  private cursorAt(): Cursor {
    const tool = this.prefs.tool;
    return { x: this.dot.x, y: this.dot.y, tool, color: this.prefs.color, size: this.prefs.sizes[tool] };
  }

  /** A gesture began: show the cursor, and with the pen down start a line. */
  private begin(): void {
    this.finish();
    this.gesture = true;
    if (this.pen) {
      this.stroke = this.nextId++;
      this.deps.sender.points(this.stroke, this.cursorAt(), [this.dot.x, this.dot.y], false);
    } else {
      this.deps.sender.cursor(this.cursorAt());
    }
  }

  /** The cursor moved to `this.dot`. */
  private moved(): void {
    if (this.stroke !== null) {
      this.deps.sender.points(this.stroke, this.cursorAt(), [this.dot.x, this.dot.y], false);
    } else {
      this.deps.sender.cursor(this.cursorAt());
    }
  }

  /** The finger lifted: the line ends, and the cursor fades on the screen. */
  private finish(): void {
    this.gesture = false;
    if (this.stroke === null) {
      return;
    }
    this.deps.sender.points(this.stroke, this.cursorAt(), [], true);
    this.stroke = null;
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
      const id = (ev as { pointerId?: number }).pointerId;
      if (id !== undefined) {
        (this.pad as HTMLElement & { setPointerCapture?: (id: number) => void }).setPointerCapture?.(id);
      }
      this.drag = pos(ev);
      this.pad.classList.add("live");
      this.begin();
    });
    this.pad.addEventListener("pointermove", (ev) => {
      if (!this.drag) {
        return;
      }
      // A phone samples a finger faster than it fires pointermove; the
      // coalesced events are the ones in between, and a curve needs them.
      const many = (ev as { getCoalescedEvents?: () => unknown[] }).getCoalescedEvents?.();
      const events = many && many.length ? many : [ev];
      const width = (this.pad as HTMLElement).clientWidth || 300;
      const speed = loadPointerPrefs(this.deps.storage).speed;
      for (const e of events) {
        const p = pos(e);
        this.dot = padMove(this.dot, p.x - this.drag.x, p.y - this.drag.y, width, speed);
        this.drag = p;
        this.moved();
      }
    });
    const end = (): void => {
      if (!this.drag) {
        return;
      }
      this.drag = null;
      this.pad.classList.remove("live");
      this.finish();
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
        this.paint();
        if (!ok) {
          setText(this.note, "Tilting was not allowed. The pad still works.");
        }
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
      this.begin();
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
      this.aimBtn.addEventListener(type, () => {
        if (this.aiming) {
          this.endAim();
          this.finish();
        }
      });
    }
  }

  /** One orientation reading while the aim button is held: the pointer's
   * aim, with the pointer's smoothing. */
  onTilt(o: Orientation): void {
    const a = this.aiming;
    if (!a) {
      return;
    }
    if (!a.base) {
      a.base = o;
      return;
    }
    const cal = this.deps.calibration?.() ?? null;
    const speed = loadPointerPrefs(this.deps.storage).speed;
    const target = cal ? aim(a.from, a.base, o, 1, cal) : aim(a.from, a.base, o, speed);
    const k = cal ? cal.smoothing : SMOOTHING;
    a.smooth = a.smooth
      ? { x: a.smooth.x + (target.x - a.smooth.x) * k, y: a.smooth.y + (target.y - a.smooth.y) * k }
      : target;
    this.dot = a.smooth;
    this.moved();
  }

  private endAim(): void {
    this.aimBtn.classList.remove("held");
    this.aiming = null;
    if (this.stopTilt) {
      this.stopTilt();
      this.stopTilt = null;
    }
  }

  // --- the tools ----------------------------------------------------------

  private paintControls(): void {
    const doc = this.doc;
    this.controls.textContent = "";
    const picker = (
      cls: string,
      options: readonly [string, string, string?][],
      current: string,
      pick: (v: string) => void,
    ): HTMLElement =>
      h(
        doc,
        "div",
        { class: "pickers " + cls },
        options.map(([value, label, icon]) => {
          const b = h(doc, "button", {
            type: "button",
            class: value === current ? "sel" : "",
            text: icon ? "" : label,
            aria: label,
            onTap: () => pick(value),
          });
          if (icon) {
            b.appendChild(h(doc, "span", { class: "ico", text: icon }));
            b.appendChild(h(doc, "span", { text: label }));
          }
          return b;
        }),
      );

    this.controls.appendChild(
      picker(
        "drawPen",
        [
          ["down", "Draw", "✍️"],
          ["up", "Move", "\u{1F446}"],
        ],
        this.pen ? "down" : "up",
        (v) => {
          this.finish();
          this.pen = v === "down";
          this.paint();
        },
      ),
    );
    this.controls.appendChild(
      picker(
        "drawTools",
        [
          ["brush", "Brush", "\u{1F58C}️"],
          ["pencil", "Pencil", "✏️"],
          ["eraser", "Eraser", "\u{1F9FD}"],
        ],
        this.prefs.tool,
        (v) => this.set({ tool: v as DrawTool }),
      ),
    );
    const tool = this.prefs.tool;
    this.controls.appendChild(
      picker(
        "drawSizes",
        [
          ["s", "Thin"],
          ["m", "Medium"],
          ["l", "Thick"],
        ],
        this.prefs.sizes[tool],
        (v) => this.set({ sizes: { ...this.prefs.sizes, [tool]: v as DrawSize } }),
      ),
    );
    if (tool !== "eraser") {
      this.controls.appendChild(
        h(
          doc,
          "div",
          { class: "swatches" },
          DRAW_COLORS.map((c) => {
            const b = h(doc, "button", {
              type: "button",
              class: c === this.prefs.color ? "sel" : "",
              aria: c,
              onTap: () => this.set({ color: c }),
            });
            b.style.background = COLOR_CSS[c];
            return b;
          }),
        ),
      );
    }
    this.controls.appendChild(
      h(doc, "button", {
        id: "drawClear",
        type: "button",
        text: "Clear the drawing",
        onTap: () => this.clear(),
      }),
    );
  }

  /** Rub out everything, everybody's. */
  clear(): void {
    this.finish();
    this.full = false;
    this.deps.sender.clear();
    this.paintNote();
  }

  private set(change: Partial<DrawPrefs>): void {
    this.prefs = { ...this.prefs, ...change };
    saveDrawPrefs(this.deps.storage, this.prefs);
    this.paintControls();
  }
}
