// The clock driver: turns /api/time round trips into samples for the ServerClock.
//
// The requests are CHAINED, never fired together -- concurrent requests queue
// behind each other on one socket and in one daemon, so a parallel burst would
// inflate the very round trip it exists to measure, and that round trip is the
// error bar on every number the clock produces. A burst runs on arrival and on
// waking (the offset gates the first sound); a slower routine sample keeps it
// fresh once locked.
import type { ApiClient } from "./net";
import type { ServerClock } from "./clock";
import type { TimingConfig } from "./config";

export interface TimeDriverDeps {
  now: () => number;
  schedule: (fn: () => void, ms: number) => void;
  /** Called when the daemon session changed, so audio can re-place itself. */
  onReset?: () => void;
}

export class TimeDriver {
  private nextAt = 0;

  constructor(
    private readonly api: ApiClient,
    private readonly clock: ServerClock,
    private readonly cfg: TimingConfig,
    private readonly deps: TimeDriverDeps,
  ) {}

  /** One round trip, timed and fed to the clock. */
  sample(): Promise<void> {
    // Pushing the next routine sample out from here keeps a burst from being
    // followed immediately by one.
    this.nextAt = this.deps.now() + this.cfg.every;
    const t0 = this.deps.now();
    return this.api
      .time()
      .then((d) => {
        const t1 = this.deps.now();
        if (typeof d.now !== "number") {
          return;
        }
        if (this.clock.adoptSession(d.session)) {
          this.deps.onReset?.();
        }
        // The answer was true somewhere between the request leaving and the
        // reply landing, so the midpoint is the estimate and half the round
        // trip is the error bar. Neither leg is assumed equal, only that
        // neither was longer than both.
        const off = d.now - (t0 + t1) / 2;
        this.clock.accept(off, t1 - t0, t1);
      })
      // Silent: the status poll already tells a listener the projector has gone
      // away, and a second message in different words is not information.
      .catch(() => {});
  }

  /** A back-to-back burst, chained through the scheduler. */
  burst(n: number): void {
    if (n <= 0) {
      return;
    }
    void this.sample().then(() => {
      this.deps.schedule(() => this.burst(n - 1), this.cfg.spacing * 1000);
    });
  }

  /** Driven off the correction tick: re-pick as of now, and sample if due. */
  tick(): void {
    this.clock.tick(this.deps.now());
    if (this.deps.now() >= this.nextAt) {
      void this.sample();
    }
  }
}
