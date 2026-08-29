// The playback buttons: play/pause, stop, cancel, the two volume nudges, and
// the two skip buttons.
//
// Every control locks itself briefly after a press, because children press
// twice and a double tap must not fire the action twice. The daemon answers a
// press with the new status, which is applied straight away rather than waited
// for on the next poll -- a button that does nothing for a second gets pressed
// again.
//
// The skip buttons are the exception, and deliberately so: tapping them
// repeatedly is how anybody uses a skip button, so they queue instead of
// locking. SeekPad owns that; see seek.ts.
import type { ApiClient, PostResult } from "./net";
import { SeekPad } from "./seek";
import { toggleClass } from "./dom";

/** The buttons, built by the views they are drawn in. The controls only add the
 * behaviour, so nothing here has to know where on the page they live. */
export interface ControlButtons {
  toggle: HTMLElement;
  stop: HTMLElement;
  /** "Never mind", on the getting-ready view. */
  cancel: HTMLElement;
  volDown: HTMLElement;
  volUp: HTMLElement;
  skipBack: HTMLElement;
  skipFwd: HTMLElement;
}

export interface ControlsDeps {
  /** The state the last poll reported, to decide pause vs resume. */
  stateOf: () => string;
  /** Apply the status a control POST returns. */
  apply: (s: import("./types").Status) => void;
  /** Drop straight to the library on stop/cancel, before the POST answers. */
  toLibrary: () => void;
  /** How many seconds are queued but not sent, for the view to preview. */
  onSeekPreview?: (pending: number) => void;
  now?: () => number;
  /** Timers, injected so the pad can be driven by a test without waiting. */
  schedule?: (fn: () => void, ms: number) => number;
  cancelTimer?: (id: number) => void;
}

export class Controls {
  /** The pending-skip state, exposed so the composition root can clear it when
   * the film goes away under a finger. */
  readonly pad: SeekPad;
  private busyUntil = 0;
  private readonly now: () => number;

  constructor(
    buttons: ControlButtons,
    private readonly api: ApiClient,
    private readonly deps: ControlsDeps,
  ) {
    this.now = deps.now ?? Date.now;

    buttons.toggle.addEventListener("click", () => {
      if (!this.guard()) {
        return;
      }
      const req =
        this.deps.stateOf() === "paused" ? this.api.resume() : this.api.pause();
      void req.then((r) => this.applyIfOk(r));
    });

    const stop = () => {
      if (!this.guard()) {
        return;
      }
      this.pad.clear();
      this.deps.toLibrary();
      void this.api.stop().then((r) => this.applyIfOk(r));
    };
    buttons.stop.addEventListener("click", stop);
    // Cancel hits the same endpoint: the daemon works out whether that means
    // abandon a warming lamp or stop a running film.
    buttons.cancel.addEventListener("click", stop);

    // Volume is not guarded -- a child holding the button down to get louder is
    // the intended use, not a double-tap to defend against.
    buttons.volDown.addEventListener("click", () => {
      void this.api.volume(-10);
    });
    buttons.volUp.addEventListener("click", () => {
      void this.api.volume(10);
    });

    this.pad = new SeekPad({
      now: this.now,
      schedule: deps.schedule ?? ((fn, ms) => window.setTimeout(fn, ms)),
      cancel: deps.cancelTimer ?? ((id) => window.clearTimeout(id)),
      onChange: (pending) => this.deps.onSeekPreview?.(pending),
      send: (delta) => {
        void this.api.seek(delta).then((r) => this.applyIfOk(r));
      },
    });
    this.hold(buttons.skipBack, -1);
    this.hold(buttons.skipFwd, 1);
  }

  /**
   * Bind one skip button.
   *
   * Pointer events rather than click, because the whole feature is the
   * difference between a press and a hold, and a click only reports that both
   * happened. preventDefault on the down stops the browser turning a long press
   * into a selection or a callout -- the page already asks for that globally,
   * but a button being held for three seconds is exactly the case where a
   * browser reconsiders.
   *
   * setPointerCapture is what makes a hold survive a thumb sliding off the
   * button, which on a round control the size of a coin is most holds. It is
   * optional-chained because the DOM stub the tests build against has no
   * business implementing it.
   */
  private hold(el: HTMLElement, dir: 1 | -1): void {
    el.addEventListener("pointerdown", (ev) => {
      const p = ev as PointerEvent;
      p.preventDefault?.();
      (el as Element & {
        setPointerCapture?: (id: number) => void;
      }).setPointerCapture?.(p.pointerId);
      toggleClass(el, "held", true);
      this.pad.press(dir);
    });
    const up = () => {
      toggleClass(el, "held", false);
      this.pad.release();
    };
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", () => {
      toggleClass(el, "held", false);
      this.pad.abort();
    });
  }

  private guard(): boolean {
    const now = this.now();
    if (now < this.busyUntil) {
      return false;
    }
    this.busyUntil = now + 800;
    return true;
  }

  private applyIfOk(r: PostResult): void {
    if (r.ok) {
      this.deps.apply(r.data as unknown as import("./types").Status);
    }
  }
}
