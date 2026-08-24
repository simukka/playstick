// The headphone audio session: the one <audio> element, the track chosen for it,
// and the loop that keeps it pinned to the film.
//
// The computational cores it stands on -- the clock offset, the timecode, the PI
// controller -- are elsewhere and separately tested. This module is the
// lifecycle: which track is loaded, when the element may start, and how each
// correction tick decides between placing, nudging, pausing and free-running.
//
// Two iOS rules shape it. Audio needs a user gesture to start, and the element
// that gets it must be the SAME one that plays later -- hence one Audio() made
// once and unlocked by the tap on "This device". And the ring/silent switch
// mutes Web Audio but not media elements, which is why every correction goes
// through playbackRate and there is no AudioContext anywhere.
import type { ServerClock } from "./clock";
import type { FilmClock } from "./timecode";
import { targetFor } from "./timecode";
import type { SyncController, SyncElement } from "./sync";
import type { Status, Track } from "./types";

/** The slice of HTMLAudioElement the session drives, beyond what sync reads. */
export interface AudioElement extends SyncElement {
  readonly paused: boolean;
  play(): Promise<void>;
  pause(): void;
  load(): void;
  getSrc(): string | null;
  setSrc(src: string): void;
  clearSrc(): void;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export type Dest = "mute" | "device";

export interface AudioDeps {
  el: AudioElement;
  clock: ServerClock;
  film: FilmClock;
  sync: SyncController;
  stamp: (path: string) => string;
  storage: StorageLike;
  /** Local monotonic clock, in SECONDS (performance.now()/1000). */
  now: () => number;
  /** Repaint the icon and the sheet after any state change. */
  onChange?: () => void;
}

export class AudioSession {
  private dest: Dest;
  private lang: string;
  private trimMs: number;

  private filmId = "";
  private tracks: Track[] = [];
  private trackN: number | null = null;
  private trackOffset = 0;
  private phoneAudio = true;
  private needGesture = false;

  constructor(private readonly deps: AudioDeps) {
    const s = deps.storage;
    this.dest = (s.getItem("ps.snd.dest") as Dest) || "mute";
    this.lang = s.getItem("ps.snd.lang") || "";
    this.trimMs = parseInt(s.getItem("ps.snd.trim") || "0", 10) || 0;
  }

  get listening(): boolean {
    return this.dest === "device";
  }
  get destination(): Dest {
    return this.dest;
  }
  get language(): string {
    return this.lang;
  }
  get trim(): number {
    return this.trimMs;
  }
  get currentTrack(): number | null {
    return this.trackN;
  }
  get availableTracks(): readonly Track[] {
    return this.tracks;
  }
  get filmReady(): boolean {
    return !!this.filmId;
  }
  get phoneAudioEnabled(): boolean {
    return this.phoneAudio;
  }
  get awaitingGesture(): boolean {
    return this.needGesture;
  }
  get playing(): boolean {
    return this.listening && this.trackN !== null && !this.deps.el.paused;
  }

  /** Fold a status poll into the session: film changes, track availability, and
   * the timeline the correction loop reads. */
  onStatus(s: Status): void {
    this.phoneAudio = s.phone_audio !== false;
    const wasFilm = this.filmId;
    this.filmId = s.id || "";
    this.tracks = s.tracks || [];
    if (this.filmId !== wasFilm) {
      this.stop();
    }

    const playable =
      this.listening &&
      this.phoneAudio &&
      !!this.filmId &&
      this.tracks.length > 0 &&
      (s.state === "playing" || s.state === "paused");
    if (!playable) {
      if (this.trackN !== null) {
        this.stop();
      }
      this.deps.film.set(null);
      this.deps.onChange?.();
      return;
    }

    if (this.trackN === null) {
      this.load(this.pickTrack());
    }

    const tc = s.timecode;
    if (!tc || typeof tc.tc !== "number") {
      // mpv has not said where it is yet -- the first second of a film. Nothing
      // is guessed; the element waits where it is.
      this.deps.onChange?.();
      return;
    }
    // A new timeline throws away only the element's PLACE; the clock offset and
    // the measured ratio are untouched. Each pause/resume/film change costs one
    // seek rather than an emptied offset window.
    if (this.deps.film.set(tc)) {
      this.deps.sync.replace();
    }

    if (this.deps.el.paused && !this.needGesture && tc.rate && this.deps.clock.hasFix()) {
      this.play();
    }
    this.deps.onChange?.();
  }

  /** One correction tick, driven at SYNC.tick. */
  correct(now: number): void {
    const el = this.deps.el;
    if (!this.listening || el.paused) {
      this.deps.sync.seekPending = true; // whatever it does next, place it
      return;
    }
    if (this.deps.film.rate === 0) {
      // The daemon says the timeline is not moving (a pause, or the demuxer
      // waiting on the NAS). Audio that plays through a stall is permanently
      // ahead afterwards, so the element stops with it.
      el.pause();
      this.deps.sync.replace();
      return;
    }
    const want = targetFor(this.deps.film.now(now), this.trackOffset, this.trimMs);
    if (want === null) {
      // No fix, or the offset aged out from under a pocketed phone. If the
      // element was never placed, park it; otherwise let it free-run on the
      // ratio, which is the one estimate a locked screen cannot take away.
      if (this.deps.sync.seekPending) {
        el.pause();
      }
      return;
    }
    this.deps.sync.correct(el, now, want, this.deps.clock.ratio);
  }

  /** The tap on "This device": the gesture iOS grants the element, so the play
   * happens inside it. */
  enableDevice(): void {
    this.setDest("device");
  }

  setDest(dest: Dest): void {
    this.dest = dest;
    this.deps.storage.setItem("ps.snd.dest", dest);
    if (dest === "device") {
      this.deps.sync.seekPending = true;
      if (this.filmId && this.tracks.length) {
        if (this.trackN === null) {
          this.load(this.pickTrack());
        }
        this.play();
      } else {
        // Nothing to play yet: unlock the element with a silent play/pause pair
        // while the gesture is still in hand.
        void this.deps.el
          .play()
          .then(() => this.deps.el.pause())
          .catch(() => {});
      }
    } else {
      this.stop();
    }
    this.deps.onChange?.();
  }

  setTrack(track: Track): void {
    this.lang = track.lang;
    this.deps.storage.setItem("ps.snd.lang", track.lang);
    if (!this.listening) {
      this.setDest("device");
    }
    if (track.n !== this.trackN) {
      this.load(track.n);
      this.play();
    }
    this.deps.onChange?.();
  }

  /** Remember a language before any film has started. */
  preferLanguage(code: string): void {
    this.lang = code;
    this.deps.storage.setItem("ps.snd.lang", code);
    this.deps.onChange?.();
  }

  nudgeTrim(deltaMs: number): void {
    this.trimMs = Math.max(-1000, Math.min(1000, this.trimMs + deltaMs));
    this.deps.storage.setItem("ps.snd.trim", String(this.trimMs));
    this.deps.sync.seekPending = true;
    this.deps.onChange?.();
  }

  /** iOS refused autoplay: the page shows a tap target, and this is its tap. */
  acceptGesture(): void {
    this.needGesture = false;
    this.deps.sync.seekPending = true;
    if (this.trackN === null && this.tracks.length) {
      this.load(this.pickTrack());
    }
    this.play();
  }

  /** The NAS lost the file under a playing track. */
  onElementError(): void {
    if (!this.listening || this.trackN === null) {
      return;
    }
    this.stop();
  }

  pickTrack(): number {
    for (const t of this.tracks) {
      if (this.lang && t.lang === this.lang) {
        return t.n;
      }
    }
    for (const t of this.tracks) {
      if (t.default) {
        return t.n;
      }
    }
    return this.tracks.length ? this.tracks[0]!.n : 0;
  }

  private load(n: number): void {
    const track = this.tracks.find((t) => t.n === n);
    if (!track || !this.filmId) {
      return;
    }
    this.trackN = n;
    this.trackOffset = track.offset || 0;
    this.deps.sync.reset(this.deps.el);
    this.deps.el.setSrc(this.deps.stamp("/api/audio/" + this.filmId + "/" + n));
    this.deps.el.load();
  }

  private play(): void {
    void this.deps.el
      .play()
      .then(() => {
        this.needGesture = false;
        this.deps.onChange?.();
      })
      .catch(() => {
        // iOS refused: reloaded page, Low Power Mode, or a per-site setting.
        this.needGesture = true;
        this.deps.onChange?.();
      });
  }

  private stop(): void {
    this.trackN = null;
    if (!this.deps.el.paused) {
      this.deps.el.pause();
    }
    if (this.deps.el.getSrc()) {
      // Drop the connection rather than parking it: the daemon holds a thread
      // per listener and only has so many.
      this.deps.el.clearSrc();
      this.deps.el.load();
    }
    this.deps.sync.replace();
  }
}
