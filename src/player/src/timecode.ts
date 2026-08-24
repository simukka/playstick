// The timecode: where the film is, on this phone's clock.
//
// The daemon publishes where the film is (`tc`), the instant on ITS clock it was
// there (`at`), whether it is moving (`rate`), and which timeline that belongs to
// (`epoch`). Composed with the ServerClock -- which already knows this phone's
// clock in the daemon's -- that is enough to say where the film is at any local
// instant without asking anybody. Nothing here reads a status poll.
import type { Timecode } from "./types";
import type { ServerClock } from "./clock";

export class FilmClock {
  private tc: Timecode | null = null;

  constructor(private readonly clock: ServerClock) {}

  /** Adopt a timeline. Returns true when the epoch changed (a seek/reset). */
  set(tc: Timecode | null): boolean {
    const changed = tc !== null && (this.tc === null || tc.epoch !== this.tc.epoch);
    this.tc = tc;
    return changed;
  }

  get epoch(): number | null {
    return this.tc ? this.tc.epoch : null;
  }

  /** 0 when the timeline is not advancing, 1 while it plays, null when unknown. */
  get rate(): number | null {
    return this.tc ? this.tc.rate : null;
  }

  /** Where the film is, right now, on this phone's clock. Null until both the
   * clock offset and a timeline exist. */
  now(local: number): number | null {
    const srv = this.clock.now(local);
    if (srv === null || this.tc === null) {
      return null;
    }
    return this.tc.tc + this.tc.rate * (srv - this.tc.at);
  }
}

/**
 * Where a particular listener's audio element should be: the film's position,
 * plus the extracted track's own origin, plus this device's headphone latency.
 * The trim is per-device because wired headphones sit ~30 ms behind the picture
 * and AirPods ~200 ms, and both people watch the same screen.
 */
export function targetFor(
  filmNow: number | null,
  trackOffset: number,
  trimMs: number,
): number | null {
  return filmNow === null ? null : filmNow + trackOffset + trimMs / 1000;
}
