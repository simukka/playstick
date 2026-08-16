// Pinning a phone's audio to the film's clock.
//
// The eye starts noticing when sound leads the picture by ~45 ms or lags by
// ~125 ms, so "closely enough" is tight. This is a controller: a loop with a
// clock, a plant (the <audio> element) and a feedback path (its currentTime).
// It is written to be testable and cheap, because it runs at TICK for the whole
// length of a film on a phone.
//
// Two gates stand between a command and the element, because writing
// playbackRate on iOS is not free -- it lands on AVPlayer.rate and re-arms the
// render pipeline, costing ~43 ms of audio. The deadband drops a write too
// small to be worth that cost; the write-spacing drops one that is merely too
// soon. Without the second, the loop feeds on its own disturbance.
//
// The measured crystal ratio (from ServerClock) is fed FORWARD, so the
// integrator no longer has to discover it -- it only mops up what the ratio
// cannot see: this phone's CPU clock, which timed the round trips, against its
// audio hardware clock, which plays the sound.
import type { SyncConfig } from "./config";
import { clamp } from "./config";

/** The slice of HTMLAudioElement the controller reads and writes. */
export interface SyncElement {
  currentTime: number;
  readonly duration: number;
  playbackRate: number;
}

export class SyncController {
  private errF: number | null = null; // smoothed error, for the P and I terms
  private drift = 0; // integrator: what the measured ratio did not cover
  private rateSet = 1; // last rate actually written to the element
  private rateAt = 0; // ...and when, so writeEvery can space writes out

  /** Set whenever the element must be placed rather than nudged. */
  seekPending = true;

  // Read by the telemetry feature; written only here.
  err = 0; // raw error last tick, for the journal and the overlay
  seeks = 0;
  rateWrites = 0;
  lastRateStep = 0; // largest single write since telemetry last cleared it

  constructor(private readonly cfg: SyncConfig) {}

  /** The rate currently commanded on the element. */
  get rate(): number {
    return this.rateSet;
  }

  /** The integrator's current contribution, a fraction. Telemetry. */
  get integrator(): number {
    return this.drift;
  }

  /**
   * The element is no longer where it belongs (a new timeline, a resume). Place
   * it next tick rather than nudging, and drop the error filter. The integrator
   * is NOT cleared: it describes two oscillators in this phone, and neither
   * changed because the film did.
   */
  replace(): void {
    this.errF = null;
    this.seekPending = true;
  }

  /**
   * A fresh track from a known rate, so the deadband compares against what the
   * element is actually doing. Drift survives, for the same reason as replace().
   */
  reset(el: SyncElement): void {
    this.errF = null;
    this.seekPending = true;
    this.rateSet = 1;
    el.playbackRate = 1;
  }

  private setRate(el: SyncElement, now: number, r: number): void {
    const { rateLimit, rateEps, writeEvery } = this.cfg;
    r = clamp(r, 1 - rateLimit, 1 + rateLimit);
    const moved = Math.abs(r - this.rateSet);
    // Too small to be worth its own cost. The error this leaves uncorrected is
    // rateEps/kp -- under a millisecond, against a 45 ms threshold.
    if (moved < rateEps) {
      return;
    }
    // ...and not more often than writeEvery, unless the command has moved far
    // enough that it is chasing something real (a resume, the far side of a
    // stall), where waiting out the interval would be the louder fault.
    if (now - this.rateAt < writeEvery && moved < rateLimit / 2) {
      return;
    }
    if (moved > this.lastRateStep) {
      this.lastRateStep = moved;
    }
    this.rateAt = now;
    this.rateSet = r;
    el.playbackRate = r;
    this.rateWrites++;
  }

  /**
   * One correction tick. `want` is the target position (film position plus the
   * track origin plus this listener's trim), or null when there is no fix yet;
   * `ratio` is the measured crystal offset from ServerClock. Placing or nudging
   * the element is the only side effect.
   *
   * Returns "seek" | "nudge" | "idle" for the tests and telemetry.
   */
  correct(
    el: SyncElement,
    now: number,
    want: number | null,
    ratio: number,
  ): "seek" | "nudge" | "idle" {
    // No fix, or the daemon has not said where the film is. The element's
    // lifecycle (pause vs free-run on the ratio) is the caller's decision; the
    // controller only declines to steer.
    if (want === null) {
      return "idle";
    }
    if (!el.duration) {
      return "idle"; // metadata has not arrived
    }
    const { seekLimit, errLp, kp, ki, tick, rateLimit, driftLimit } = this.cfg;
    const clampedWant = clamp(want, 0, el.duration);
    this.err = el.currentTime - clampedWant;

    if (this.seekPending || Math.abs(this.err) > seekLimit) {
      // Rare by design: the start of a track, a resume, the far side of a
      // stall. Every hard seek is an audible cut and a fresh random read across
      // the NAS, so the nudge below does the routine work.
      el.currentTime = clampedWant;
      this.seekPending = false;
      this.errF = null;
      this.seeks++;
      return "seek";
    }

    // iOS reports currentTime on decoded-frame boundaries, so at 4 Hz it is a
    // staircase. kp would put that quantisation straight into the rate; the raw
    // err above is kept for the seek test, where a real desync must not be
    // smoothed away.
    this.errF =
      this.errF === null ? this.err : this.errF + errLp * (this.err - this.errF);

    // Conditional integration: only accumulate when it would move the command
    // or bring it back off the clamp. Winding into a saturated command only
    // buys time that has to be unwound later, during which the loop is deaf.
    const wantDrift = this.drift + this.errF * ki * tick;
    const cmd = 1 + ratio - wantDrift - this.errF * kp;
    if (Math.abs(cmd - 1) < rateLimit || Math.abs(wantDrift) < Math.abs(this.drift)) {
      this.drift = clamp(wantDrift, -driftLimit, driftLimit);
    }
    this.setRate(el, now, 1 + ratio - this.drift - this.errF * kp);
    return "nudge";
  }

  /**
   * Going into a pocket: drop the proportional term and leave the element
   * running on the crystal ratio alone, because that ratio is the only estimate
   * a locked screen cannot take away.
   */
  coast(el: SyncElement, now: number, ratio: number): void {
    this.setRate(el, now, 1 + ratio - this.drift);
  }

  /** Clears the largest-write high-water mark; the telemetry line owns the reset. */
  takeRateStep(): number {
    const step = this.lastRateStep;
    this.lastRateStep = 0;
    return step;
  }
}
