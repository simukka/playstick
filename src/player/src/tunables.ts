// The controller constants, made editable from the phone under ?debug.
//
// The reason this exists: the fault being chased is an iPhone's audio pipeline
// on somebody else's Wi-Fi, and the loop between "change a number" and "hear
// whether it helped" used to be an Ansible run, a service restart and a reload.
// It is now a tap. Two constants have already been set wrong from the armchair,
// and neither was findable without a real device making a real sound.
//
// Each tunable binds get/set straight onto the live config object, so editing
// one changes what the controllers read on their very next tick -- no copy of
// the value to keep in step. Display units are the telemetry's: ppm and ms, so
// a number read off the journal can be typed straight back in.
import type { ClockConfig, SyncConfig, TimingConfig } from "./config";
import { clamp } from "./config";

export interface Tunable {
  code: string; // short key for the telemetry digest
  name: string; // the source constant name
  label: string;
  unit: string;
  scale: number; // multiply the internal value by this for display
  min: number; // in display units
  max: number;
  step: number;
  hint: string;
  get(): number; // internal units
  set(v: number): void;
  def: number; // internal units, captured at construction
  onSet?: () => void; // e.g. re-arm the tick when the period changes
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export class Tunables {
  readonly items: Tunable[];

  constructor(
    sync: SyncConfig,
    clock: ClockConfig,
    timing: TimingConfig,
    onTickChange?: () => void,
  ) {
    const t = (
      code: string,
      name: string,
      label: string,
      unit: string,
      scale: number,
      min: number,
      max: number,
      step: number,
      hint: string,
      get: () => number,
      set: (v: number) => void,
      onSet?: () => void,
    ): Tunable => ({ code, name, label, unit, scale, min, max, step, hint, get, set, def: get(), ...(onSet ? { onSet } : {}) });

    this.items = [
      t("sl", "seekLimit", "Seek threshold", "ms", 1000, 50, 3000, 50,
        "Error that gets a hard cut instead of a nudge. Must stay above what a seek costs (~250 ms) or every seek recreates the error that triggered it.",
        () => sync.seekLimit, (v) => (sync.seekLimit = v)),
      t("kp", "kp", "Proportional gain", "ppm/ms", 1000, 0, 400, 5,
        "Rate commanded per millisecond of error.",
        () => sync.kp, (v) => (sync.kp = v)),
      t("ki", "ki", "Integral gain", "ppm/ms/s", 1000, 0, 20, 0.5,
        "How fast the residual accumulates; the measured ratio does most of this job now.",
        () => sync.ki, (v) => (sync.ki = v)),
      t("re", "rateEps", "Write deadband", "ppm", 1e6, 0, 5000, 50,
        "Rate change too small to be worth writing; every write re-arms the iOS pipeline.",
        () => sync.rateEps, (v) => (sync.rateEps = v)),
      t("rl", "rateLimit", "Rate clamp", "ppm", 1e6, 2000, 50000, 1000,
        "Never bend playback further than this.",
        () => sync.rateLimit, (v) => (sync.rateLimit = v)),
      t("dl", "driftLimit", "Integrator clamp", "ppm", 1e6, 50, 20000, 50,
        "How far the integrator alone may bend playback.",
        () => sync.driftLimit, (v) => (sync.driftLimit = v)),
      t("we", "writeEvery", "Write spacing", "s", 1, 0, 60, 1,
        "Least time between writes to playbackRate; spacing them is what stops the loop feeding itself.",
        () => sync.writeEvery, (v) => (sync.writeEvery = v)),
      t("el", "errLp", "Error smoothing", "%", 100, 1, 100, 1,
        "Weight given to each fresh error sample; 100% is no filter.",
        () => sync.errLp, (v) => (sync.errLp = v)),
      t("te", "every", "Clock sample interval", "s", 1, 1, 60, 1,
        "Seconds between /api/time samples once locked.",
        () => timing.every, (v) => (timing.every = v)),
      t("oa", "offMaxAge", "Offset shelf life", "s", 1, 5, 300, 5,
        "How old the winning clock sample may be before it is dropped.",
        () => clock.offMaxAge, (v) => (clock.offMaxAge = v)),
      t("rw", "ratioSpan", "Ratio fit window", "s", 1, 30, 600, 30,
        "Seconds of clock samples the crystal ratio is fitted through.",
        () => clock.ratioSpan, (v) => (clock.ratioSpan = v)),
      t("tk", "tick", "Correction interval", "ms", 1000, 50, 1000, 50,
        "How often the loop runs; also the stall window.",
        () => sync.tick, (v) => (sync.tick = v), onTickChange),
      t("st", "stall", "Stall threshold", "ms", 1000, 5, 500, 5,
        "Shortfall counted as a stall rather than jitter. Telemetry only.",
        () => sync.stall, (v) => (sync.stall = v)),
    ];
  }

  private decimals(t: Tunable): number {
    return t.step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log(t.step) / Math.LN10));
  }

  value(t: Tunable): number {
    return t.get() * t.scale;
  }

  text(t: Tunable): string {
    const s = this.value(t).toFixed(this.decimals(t));
    return t.unit ? s + " " + t.unit : s;
  }

  shippedText(t: Tunable): string {
    const s = (t.def * t.scale).toFixed(this.decimals(t));
    return t.unit ? s + " " + t.unit : s;
  }

  changed(t: Tunable): boolean {
    return t.get() !== t.def;
  }

  apply(t: Tunable, shown: number): void {
    // Round onto the step grid before storing so repeated taps cannot
    // accumulate float dust into a value the display no longer matches.
    let v = Math.round(clamp(shown, t.min, t.max) / t.step) * t.step;
    v = parseFloat(v.toFixed(this.decimals(t) + 3));
    t.set(v / t.scale);
    t.onSet?.();
  }

  step(t: Tunable, direction: number): void {
    this.apply(t, this.value(t) + direction * t.step);
  }

  reset(): void {
    for (const t of this.items) {
      t.set(t.def);
      t.onSet?.();
    }
  }

  /** Load overrides from storage, but only when ?debug is on: a number tuned on
   * one phone must not silently persist into a build that exists nowhere. */
  load(storage: StorageLike, enabled: boolean): void {
    if (!enabled) {
      return;
    }
    let saved: Record<string, number> = {};
    try {
      saved = JSON.parse(storage.getItem("ps.snd.tune") || "{}");
    } catch {
      saved = {};
    }
    for (const t of this.items) {
      const v = saved[t.name];
      if (typeof v === "number" && isFinite(v)) {
        this.apply(t, v);
      }
    }
  }

  save(storage: StorageLike): void {
    const saved: Record<string, number> = {};
    for (const t of this.items) {
      if (this.changed(t)) {
        saved[t.name] = this.value(t);
      }
    }
    try {
      if (Object.keys(saved).length) {
        storage.setItem("ps.snd.tune", JSON.stringify(saved));
      } else {
        storage.removeItem("ps.snd.tune");
      }
    } catch {
      /* private browsing: the tuning just will not survive a reload */
    }
  }

  /** Which numbers were NOT the shipped ones, for the journal. Empty on a stock
   * build, and the difference between a capture readable six weeks later and one
   * that is not. */
  digest(): string {
    const parts: string[] = [];
    for (const t of this.items) {
      if (this.changed(t)) {
        parts.push(t.code + ":" + this.value(t).toFixed(this.decimals(t)));
      }
    }
    return parts.join(",");
  }
}
