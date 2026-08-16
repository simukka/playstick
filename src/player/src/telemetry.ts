// The ?debug telemetry: one header on every status poll, so a film watched on a
// real phone over real Wi-Fi leaves a record in the journal.
//
// The on-screen overlay can only be read by somebody holding the phone and only
// shows now; this is the same numbers, timestamped, for a whole film. What is
// sent is PEAKS and COUNTS since the previous poll -- one dropout in four ticks
// is the thing being hunted, and an average would hide exactly that. Times are
// milliseconds and rates are ppm, both integers, because that is the resolution
// any of this is good to and it keeps one journal line readable at a glance.

export interface TelemetrySnapshot {
  now: number; // seconds since page load
  listening: boolean;
  hasSrc: boolean;
  paused: boolean;
  hidden: boolean;
  currentTime: number;
  errMs: number; // signed: which side of the picture the sound is on
  ratePpm: number; // commanded rate offset
  ratioPpm: number; // measured crystal ratio
  driftPpm: number; // integrator residual
  offsetMs: number | null; // this clock in the daemon's, minus local
  rttMs: number | null;
  samples: number;
  epoch: number | null;
  writes: number; // cumulative rate writes
  seeks: number; // cumulative hard seeks
  waits: number; // cumulative element stalls (waiting/stalled)
  buffering: number; // cumulative buffering polls
  digest: string; // which tunables differ from shipped
}

function num(v: number | null, places: number): string {
  return typeof v === "number" && isFinite(v) ? v.toFixed(places) : "";
}

export class Telemetry {
  private readonly id: string;
  private base = { writes: 0, seeks: 0, waits: 0, buffering: 0 };
  private errPeak = 0; // signed peak error since the last line

  constructor(idGen: () => string = () => Math.random().toString(16).slice(2, 8)) {
    this.id = idGen();
  }

  /** Fold in a per-tick error sample, so the line reports the worst the poll
   * interval saw rather than the one instant it happened to look. */
  recordErrMs(errMs: number): void {
    if (Math.abs(errMs) > Math.abs(this.errPeak)) {
      this.errPeak = errMs;
    }
  }

  /** Build the header for this poll and reset the deltas it consumed. */
  build(s: TelemetrySnapshot): string {
    const head = `v=2;id=${this.id};t=${s.now.toFixed(1)}`;
    if (!s.listening) {
      return head + ";st=off";
    }
    if (!s.hasSrc) {
      return head + ";st=idle";
    }
    const line =
      head +
      ";st=" +
      (s.paused ? "pause" : "play") +
      ";hid=" +
      (s.hidden ? 1 : 0) +
      ";ct=" +
      num(s.currentTime, 2) +
      ";err=" +
      num(s.errMs, 0) +
      ";errp=" +
      num(this.errPeak, 0) +
      ";rate=" +
      num(s.ratePpm, 0) +
      ";ratio=" +
      num(s.ratioPpm, 0) +
      ";drift=" +
      num(s.driftPpm, 0) +
      ";off=" +
      num(s.offsetMs, 1) +
      ";ort=" +
      num(s.rttMs, 0) +
      ";ns=" +
      s.samples +
      ";ep=" +
      (s.epoch === null ? "" : s.epoch) +
      ";w=" +
      (s.writes - this.base.writes) +
      ";sk=" +
      (s.seeks - this.base.seeks) +
      ";wt=" +
      (s.waits - this.base.waits) +
      ";bf=" +
      (s.buffering - this.base.buffering) +
      ";tun=" +
      s.digest;

    this.base = { writes: s.writes, seeks: s.seeks, waits: s.waits, buffering: s.buffering };
    this.errPeak = 0;
    return line;
  }
}
