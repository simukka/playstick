// The playback buttons: play/pause, stop, cancel, and the two volume nudges.
//
// Every control locks itself briefly after a press, because children press
// twice and a double tap must not fire the action twice. The daemon answers a
// press with the new status, which is applied straight away rather than waited
// for on the next poll -- a button that does nothing for a second gets pressed
// again.
import type { ApiClient, PostResult } from "./net";

/** The buttons, built by the views they are drawn in. The controls only add the
 * behaviour, so nothing here has to know where on the page they live. */
export interface ControlButtons {
  toggle: HTMLElement;
  stop: HTMLElement;
  /** "Never mind", on the getting-ready view. */
  cancel: HTMLElement;
  volDown: HTMLElement;
  volUp: HTMLElement;
}

export interface ControlsDeps {
  /** The state the last poll reported, to decide pause vs resume. */
  stateOf: () => string;
  /** Apply the status a control POST returns. */
  apply: (s: import("./types").Status) => void;
  /** Drop straight to the library on stop/cancel, before the POST answers. */
  toLibrary: () => void;
  now?: () => number;
}

export class Controls {
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
