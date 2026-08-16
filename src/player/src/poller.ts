// The status heartbeat.
//
// One poll in flight at a time: visibilitychange calls poll() directly to
// resync a phone coming out of a pocket, and two overlapping chains would
// double every request for the rest of the film -- so each poll cancels the
// pending tick before it starts. The cadence follows what is on screen.
import type { ApiClient } from "./net";
import type { PlayerState, Status } from "./types";

/**
 * How long until the next poll. Faster while something is happening (the steps
 * of a preparing film are what a child is watching, and a three-second gap makes
 * the sequence look stuck), slowest in a pocket where the correction loop is not
 * running and polling harder only costs battery.
 */
export function pollDelay(state: PlayerState, hidden: boolean): number {
  if (hidden) {
    return 5000;
  }
  if (state === "playing" || state === "paused" || state === "preparing") {
    return 1000;
  }
  return 3000;
}

export interface PollerDeps {
  now: () => number;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  hidden: () => boolean;
  stateOf: () => PlayerState;
  onStatus: (s: Status, rttSec: number) => void;
  onOutage: () => void;
  header?: () => string | undefined;
}

export class Poller {
  private timer: number | null = null;

  constructor(
    private readonly api: ApiClient,
    private readonly deps: PollerDeps,
  ) {}

  poll = (): void => {
    if (this.timer !== null) {
      this.deps.cancel(this.timer);
      this.timer = null;
    }
    const sent = this.deps.now();
    const header = this.deps.header?.();
    this.api
      .status(header)
      .then((s) => {
        this.deps.onStatus(s, (this.deps.now() - sent) / 1000);
      })
      .catch(() => {
        this.deps.onOutage();
      })
      .then(() => {
        const delay = pollDelay(this.deps.stateOf(), this.deps.hidden());
        this.timer = this.deps.schedule(this.poll, delay);
      });
  };
}
