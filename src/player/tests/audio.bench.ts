import { bench, describe } from "vitest";
import { AudioSession, type AudioElement, type StorageLike } from "../src/audio";
import { ServerClock } from "../src/clock";
import { FilmClock } from "../src/timecode";
import { SyncController } from "../src/sync";
import { CLOCK, SYNC } from "../src/config";
import type { Status, Track } from "../src/types";

class FakeAudio implements AudioElement {
  currentTime = 0;
  duration = 7200;
  playbackRate = 1;
  paused = false;
  private src: string | null = "x";
  play(): Promise<void> {
    this.paused = false;
    return Promise.resolve();
  }
  pause(): void {
    this.paused = true;
  }
  load(): void {}
  getSrc(): string | null {
    return this.src;
  }
  setSrc(s: string): void {
    this.src = s;
  }
  clearSrc(): void {
    this.src = null;
  }
}

class NullStorage implements StorageLike {
  getItem(): string | null {
    return null;
  }
  setItem(): void {}
  removeItem(): void {}
}

const TRACKS: Track[] = [{ n: 0, lang: "eng", default: true }];

// The full per-tick audio path: film projection, target, and the controller,
// as it runs at SYNC.tick for a whole film. Guards the composed hot path, not
// just the controller the sync bench already covers.
describe("AudioSession.correct", () => {
  bench("listening, steady nudge", () => {
    const el = new FakeAudio();
    const clock = new ServerClock(CLOCK);
    clock.adoptSession("s1");
    clock.accept(0, 0.003, 1000);
    const film = new FilmClock(clock);
    const sync = new SyncController(SYNC);
    const s = new AudioSession({
      el,
      clock,
      film,
      sync,
      stamp: (p) => p,
      storage: new NullStorage(),
      now: () => 1000,
    });
    s.setDest("device");
    s.onStatus({
      state: "playing",
      id: "f",
      tracks: TRACKS,
      timecode: { tc: 100, at: 1000, rate: 1, epoch: 1 },
    } as Status);
    let now = 1000;
    for (let k = 0; k < 100000; k++) {
      now += SYNC.tick;
      el.currentTime += el.playbackRate * SYNC.tick;
      s.correct(now);
    }
  });
});
