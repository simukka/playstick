// The controller constants, in one place and one unit system: seconds for time,
// a fraction for a rate (0.02 = 2%). The phone-facing debug sheet edits these in
// ppm and ms, but that is a display concern and lives with the sheet, not here.
//
// The values are inherited from the shipped page, where each was measured on a
// real device against real Wi-Fi rather than chosen. The comments that justify
// them live next to the code that reads them; this file is only their home.

export interface ClockConfig {
  /** Seconds of samples the crystal-ratio slope is fitted through. */
  ratioSpan: number;
  /** Span below which there is no honest slope; the last one stands. */
  ratioMin: number;
  /** An offset older than this is not trusted for placing the element. */
  offMaxAge: number;
  /** A round trip slower than 2*best+this is noise for the slope. */
  rttSlack: number;
  /** Hard clamp on the fitted ratio, a fraction. */
  rateLimit: number;
}

export interface SyncConfig {
  /** Error (s) that earns a hard seek instead of a nudge. */
  seekLimit: number;
  /** Proportional gain: commanded rate per second of error. */
  kp: number;
  /** Integral gain per tick. */
  ki: number;
  /** Never bend playback further than this, a fraction. */
  rateLimit: number;
  /** How far the integrator alone may bend playback, a fraction. */
  driftLimit: number;
  /** A rate change smaller than this is not worth an audible write. */
  rateEps: number;
  /** Least seconds between writes to playbackRate. */
  writeEvery: number;
  /** EMA weight on a fresh error sample. */
  errLp: number;
  /** Seconds per correction tick. */
  tick: number;
  /** Shortfall (s) over one tick counted as a stall, not jitter. */
  stall: number;
}

export interface TimingConfig {
  /** /api/time requests fired back to back on arrival and on waking. */
  burst: number;
  /** Seconds between the requests in a burst. */
  spacing: number;
  /** Seconds between routine samples once locked. */
  every: number;
}

export const CLOCK: ClockConfig = {
  ratioSpan: 180,
  ratioMin: 30,
  offMaxAge: 30,
  rttSlack: 0.002,
  rateLimit: 0.02,
};

export const SYNC: SyncConfig = {
  seekLimit: 0.25,
  kp: 0.15,
  ki: 0.002,
  rateLimit: 0.02,
  driftLimit: 0.0005,
  rateEps: 0.0001,
  writeEvery: 15,
  errLp: 0.1,
  tick: 0.25,
  stall: 0.03,
};

export const TIMING: TimingConfig = {
  burst: 8,
  spacing: 0.08,
  every: 5,
};

export function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}
