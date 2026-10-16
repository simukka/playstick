// Skipping: what a press is worth, and how presses turn into one request.
//
// The rule the rest of the page enforces is that nothing may lose your place:
// there is no scrub bar and there never will be. Skipping is the other half of
// that bargain -- a control you can only move by amounts you asked for.
//
// A press is ten seconds. Holding ramps it exponentially, so three seconds on
// the thumb is half an hour of film, which is what "skip to about the middle"
// actually means to somebody standing in a dark room. The ramp is previewed and
// committed once on release rather than fired repeatedly while held: every seek
// is a real mpv seek and a fresh random read across CIFS on a box whose one
// Wi-Fi radio is already carrying the film and everybody's headphone audio.

/** One press, before any hold. */
export const STEP = 10;

/** The ramp's time constant, in ms. Chosen, not tuned: it is the number that
 * makes 10 s at t=0 become 1800 s at t=3 s, which is the stated requirement --
 * hold the forward button for three seconds and go half an hour. It works out
 * as a doubling every 400 ms. */
const TAU = 3000 / Math.log(180);

/** Where the ramp stops. A film is the real limit and the daemon clamps to it,
 * but the label is drawn from this number and a forgotten thumb should not be
 * able to make it read a day and a half. */
const MAX_JUMP = 4 * 3600;

/** How close to the end of a film a forward skip can land. The daemon's own
 * SEEK_TAIL, repeated here so the preview promises what the daemon will do --
 * mpv runs with --keep-open=no, so a seek to the very end is the film ending
 * rather than a skip, and neither side may offer that as a place to go. */
export const TAIL = 5;

/** Repaint cadence while held. Fast enough that the number never looks stepped,
 * slow enough that it is not a render per frame on a phone that is also
 * decoding audio. */
export const TICK = 60;

/** How long after the last press to wait before sending.
 *
 * This is the whole reason tapping a skip button three times costs one seek of
 * thirty seconds rather than three of ten. Repeated tapping is not misuse, it
 * is how anybody uses a skip button, and each tap left to itself would be a
 * round trip and a re-buffer. It also replaces the 800 ms double-tap lock the
 * other controls use, which here would simply swallow the second tap. */
export const FLUSH = 350;

/** Round a jump to something a person can read back off the screen: five
 * seconds under a minute, half-minutes under ten, whole minutes above. An
 * unrounded exponential reads "+7:23" and tells you nothing you can aim with. */
function nice(seconds: number): number {
  if (seconds < 60) {
    return Math.round(seconds / 5) * 5;
  }
  if (seconds < 600) {
    return Math.round(seconds / 30) * 30;
  }
  return Math.round(seconds / 60) * 60;
}

/** How many seconds a press held for `heldMs` is worth. */
export function jumpFor(heldMs: number): number {
  const raw = STEP * Math.exp(Math.max(0, heldMs) / TAU);
  return nice(Math.min(raw, MAX_JUMP));
}

export interface SeekPadDeps {
  now: () => number;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  /** The pending total changed: redraw the preview. */
  onChange: (pending: number) => void;
  /** Commit, once. */
  send: (delta: number) => void;
}

/**
 * The two skip buttons' shared state: one pending number of seconds, and one
 * timer that turns it into one request.
 *
 * Deliberately knows nothing about elements or events. It is driven by
 * press/release/abort, which is what a pointer gesture reduces to, and it is
 * the whole of the behaviour worth testing.
 */
export class SeekPad {
  /** Seconds queued but not yet sent. Signed. Zero when there is nothing
   * pending, which is what the view draws its preview from. */
  pending = 0;

  private dir = 0;
  private base = 0;
  private since = 0;
  private ticker: number | null = null;
  private flusher: number | null = null;

  constructor(private readonly deps: SeekPadDeps) {}

  /** True while a button is down. */
  get holding(): boolean {
    return this.dir !== 0;
  }

  press(dir: 1 | -1): void {
    if (this.dir !== 0) {
      // A second finger on the other button. The first one owns the ramp;
      // ignoring this is better than restarting it under a thumb that never
      // moved.
      return;
    }
    this.stopFlush();
    this.dir = dir;
    // The ramp adds to whatever was already queued, so tapping twice and then
    // holding continues from where the taps left off rather than discarding
    // them.
    this.base = this.pending;
    this.since = this.deps.now();
    this.set(this.base + dir * STEP);
    this.ticker = this.deps.schedule(() => this.tick(), TICK);
  }

  release(): void {
    if (this.dir === 0) {
      return;
    }
    this.stopTick();
    this.set(this.base + this.dir * jumpFor(this.deps.now() - this.since));
    this.dir = 0;
    this.armFlush();
  }

  /** The gesture was taken away -- a call arrived, the browser cancelled the
   * pointer. Whatever the hold had grown to is discarded rather than committed:
   * an accidental half-hour jump is much worse than a lost one. */
  abort(): void {
    if (this.dir === 0) {
      return;
    }
    this.stopTick();
    this.dir = 0;
    this.set(this.base);
    if (this.pending !== 0) {
      this.armFlush();
    }
  }

  /** Drop everything without sending -- the film ended, or the view went away
   * under a finger. */
  clear(): void {
    this.stopTick();
    this.stopFlush();
    this.dir = 0;
    this.set(0);
  }

  private tick(): void {
    if (this.dir === 0) {
      return;
    }
    this.set(this.base + this.dir * jumpFor(this.deps.now() - this.since));
    this.ticker = this.deps.schedule(() => this.tick(), TICK);
  }

  private set(pending: number): void {
    if (pending === this.pending) {
      return;
    }
    this.pending = pending;
    this.deps.onChange(pending);
  }

  private armFlush(): void {
    this.stopFlush();
    this.flusher = this.deps.schedule(() => {
      this.flusher = null;
      const delta = this.pending;
      this.set(0);
      if (delta) {
        this.deps.send(delta);
      }
    }, FLUSH);
  }

  private stopTick(): void {
    if (this.ticker !== null) {
      this.deps.cancel(this.ticker);
      this.ticker = null;
    }
  }

  private stopFlush(): void {
    if (this.flusher !== null) {
      this.deps.cancel(this.flusher);
      this.flusher = null;
    }
  }
}
