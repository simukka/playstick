// This phone's clock, expressed in the daemon's.
//
// "What time is it there" is measured on its own route (/api/time), separate
// from "where is the film", because one question that answered both -- a polled
// position subtracted from a local clock -- makes a slow poll look identical to
// a film that moved. /api/time takes no lock and touches no disk, so the round
// trip it measures is the wire and nothing else.
//
// Two facts come out of the same window of samples:
//   offset  the quickest RECENT exchange -- where this clock sits now
//   ratio   the SLOPE through all of them -- this crystal against the daemon's,
//           which is what keeps a pocketed phone (page timers suspended, one
//           <audio> element still playing) in sync with no correction running.
//
// The rewrite's performance concern lives here: this is touched on every time
// sample and every correction tick, on a phone. So the samples are three
// parallel Float64Arrays used as a time-ordered ring -- no per-sample object,
// no array reallocation on prune, and the fit is two straight passes with no
// scratch array. Contrast the shipped page, which rebuilt two arrays on every
// accept and every tick.
import type { ClockConfig } from "./config";
import { clamp } from "./config";

export class ServerClock {
  private readonly off: Float64Array;
  private readonly rtt: Float64Array;
  private readonly at: Float64Array;
  private readonly cap: number;
  private head = 0;
  private len = 0;

  private best = -1; // ring index of the offset in use, or -1 for no fix
  private _ratio = 0;
  private _session = "";

  constructor(
    private readonly cfg: ClockConfig,
    capacity = 512,
  ) {
    this.cap = capacity;
    this.off = new Float64Array(capacity);
    this.rtt = new Float64Array(capacity);
    this.at = new Float64Array(capacity);
  }

  /** Daemon seconds per phone second, as a rate offset (0 == identical). */
  get ratio(): number {
    return this._ratio;
  }

  get session(): string {
    return this._session;
  }

  get samples(): number {
    return this.len;
  }

  hasFix(): boolean {
    return this.best >= 0;
  }

  /** The round trip of the offset currently in use, or null. Telemetry only. */
  bestRtt(): number | null {
    return this.best >= 0 ? this.rtt[this.best]! : null;
  }

  /**
   * A different run of the daemon. Its clock counts from a new origin, so every
   * offset, the slope through them, and any timeline read off them is now wrong
   * by an unbounded amount. Everything is dropped. Returns true when it reset,
   * so the caller can force the audio element to re-place itself.
   */
  adoptSession(id: string): boolean {
    if (id === this._session) {
      return false;
    }
    this._session = id;
    this.head = 0;
    this.len = 0;
    this.best = -1;
    this._ratio = 0;
    return true;
  }

  /**
   * Record one round trip. `off` is the daemon's clock minus the local midpoint
   * of the exchange; `rtt` is the round trip; `at` is the local instant it
   * arrived. Samples must arrive in non-decreasing `at`.
   */
  accept(off: number, rtt: number, at: number): void {
    const i = (this.head + this.len) % this.cap;
    if (this.len === this.cap) {
      this.head = (this.head + 1) % this.cap; // full: drop the oldest
    } else {
      this.len++;
    }
    this.off[i] = off;
    this.rtt[i] = rtt;
    this.at[i] = at;
    this.recompute(at);
  }

  /**
   * Re-choose the offset and re-fit the slope as of `now`, without a new
   * sample. Driven off the correction tick so a stale offset ages out on the
   * clock rather than waiting for a reply that a pocketed phone will not get.
   */
  tick(now: number): void {
    this.recompute(now);
  }

  /** This phone's clock read in the daemon's, or null with no fix. */
  now(local: number): number | null {
    if (this.best < 0) {
      return null;
    }
    return local + this.off[this.best]! + this._ratio * (local - this.at[this.best]!);
  }

  private recompute(now: number): void {
    const { ratioSpan, ratioMin, offMaxAge, rttSlack, rateLimit } = this.cfg;

    // Prune the front of the ring: samples arrive time-ordered, so anything
    // past the fit window is at the head and leaves without touching the rest.
    while (this.len > 0 && now - this.at[this.head]! > ratioSpan) {
      this.head = (this.head + 1) % this.cap;
      this.len--;
    }

    // The offset: quickest round trip among the RECENT samples. Delay is
    // one-sided -- a packet is held up, never hurried -- so the fastest
    // exchange has the least room to be wrong, and averaging it against slower
    // ones can only move it off the truth. NTP's rule.
    let best = -1;
    let bestRtt = Infinity;
    for (let k = 0; k < this.len; k++) {
      const idx = (this.head + k) % this.cap;
      if (now - this.at[idx]! > offMaxAge) {
        continue;
      }
      if (this.rtt[idx]! < bestRtt) {
        bestRtt = this.rtt[idx]!;
        best = idx;
      }
    }
    this.best = best;
    if (best < 0) {
      return;
    }

    // The ratio: least-squares slope of offset against time, over the samples
    // clean enough to fit -- a round trip four times the wire carries four
    // times the uncertainty, and at the end of a long baseline that is leverage
    // in the wrong direction. Two passes, no scratch array: first the means,
    // then the covariance and variance.
    const cut = 2 * bestRtt + rttSlack;
    let n = 0;
    let mx = 0;
    let my = 0;
    let first = -1;
    let last = -1;
    for (let k = 0; k < this.len; k++) {
      const idx = (this.head + k) % this.cap;
      if (this.rtt[idx]! > cut) {
        continue;
      }
      if (first < 0) {
        first = idx;
      }
      last = idx;
      mx += this.at[idx]!;
      my += this.off[idx]!;
      n++;
    }
    if (n < 3) {
      return;
    }
    // Below this span there is no honest slope, and the last one stands -- which
    // is exactly what a phone coming out of a pocket needs: the ratio it
    // measured before the screen locked is as true as it was.
    if (this.at[last]! - this.at[first]! < ratioMin) {
      return;
    }
    mx /= n;
    my /= n;
    let num = 0;
    let den = 0;
    for (let k = 0; k < this.len; k++) {
      const idx = (this.head + k) % this.cap;
      if (this.rtt[idx]! > cut) {
        continue;
      }
      const dx = this.at[idx]! - mx;
      num += dx * (this.off[idx]! - my);
      den += dx * dx;
    }
    if (den > 0) {
      this._ratio = clamp(num / den, -rateLimit, rateLimit);
    }
  }
}
