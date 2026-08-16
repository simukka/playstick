import { describe, it, expect } from "vitest";
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
  paused = true;
  private src: string | null = null;
  rejectPlay = false;
  loads = 0;

  play(): Promise<void> {
    if (this.rejectPlay) {
      return Promise.reject(new Error("NotAllowed"));
    }
    this.paused = false;
    return Promise.resolve();
  }
  pause(): void {
    this.paused = true;
  }
  load(): void {
    this.loads++;
  }
  getSrc(): string | null {
    return this.src;
  }
  setSrc(src: string): void {
    this.src = src;
  }
  clearSrc(): void {
    this.src = null;
  }
}

class FakeStorage implements StorageLike {
  private d: Record<string, string> = {};
  constructor(seed: Record<string, string> = {}) {
    this.d = { ...seed };
  }
  getItem(k: string): string | null {
    return k in this.d ? this.d[k]! : null;
  }
  setItem(k: string, v: string): void {
    this.d[k] = v;
  }
  removeItem(k: string): void {
    delete this.d[k];
  }
}

const flush = () => new Promise((r) => setTimeout(r, 0));

function session(seed: Record<string, string> = {}) {
  const el = new FakeAudio();
  const clock = new ServerClock(CLOCK);
  clock.adoptSession("s1");
  clock.accept(0, 0.003, 1000); // a flat, immediate fix
  const film = new FilmClock(clock);
  const sync = new SyncController(SYNC);
  let changes = 0;
  const s = new AudioSession({
    el,
    clock,
    film,
    sync,
    stamp: (p) => p + "?v=b",
    storage: new FakeStorage(seed),
    now: () => 1000,
    onChange: () => changes++,
  });
  return { s, el, clock, film, sync, get changes() { return changes; } };
}

const TRACKS: Track[] = [
  { n: 0, lang: "eng", default: true },
  { n: 1, lang: "fin", offset: 0.5 },
];

function status(over: Partial<Status>): Status {
  return { state: "playing", id: "film1", tracks: TRACKS, ...over } as Status;
}

describe("AudioSession destination", () => {
  it("does nothing audible while muted", async () => {
    const h = session();
    h.s.onStatus(status({ timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 } }));
    await flush();
    expect(h.s.currentTrack).toBeNull();
    expect(h.el.getSrc()).toBeNull();
  });

  it("loads and starts a track once switched to this device", async () => {
    const h = session();
    h.s.setDest("device"); // the gesture, with no film yet
    await flush(); // the silent unlock play/pause settles before the next poll
    h.s.onStatus(status({ timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 } }));
    await flush();
    expect(h.s.currentTrack).toBe(0); // default track
    expect(h.el.getSrc()).toBe("/api/audio/film1/0?v=b");
    expect(h.el.paused).toBe(false);
  });

  it("honours a remembered language when picking a track", async () => {
    const h = session({ "ps.snd.dest": "device", "ps.snd.lang": "fin" });
    h.s.onStatus(status({ timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 } }));
    await flush();
    expect(h.s.currentTrack).toBe(1);
  });
});

describe("AudioSession film lifecycle", () => {
  it("stops the old track when the film changes", async () => {
    const h = session({ "ps.snd.dest": "device" });
    h.s.onStatus(status({ id: "film1", timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 } }));
    await flush();
    expect(h.s.currentTrack).toBe(0);
    h.s.onStatus(status({ id: "film2", timecode: { tc: 0, at: 1000, rate: 1, epoch: 2 } }));
    await flush();
    // stop() cleared the track, then the new film loaded its default.
    expect(h.el.getSrc()).toBe("/api/audio/film2/0?v=b");
  });

  it("stops when the film ends", async () => {
    const h = session({ "ps.snd.dest": "device" });
    h.s.onStatus(status({ timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 } }));
    await flush();
    h.s.onStatus(status({ state: "idle", id: "", tracks: [] }));
    expect(h.s.currentTrack).toBeNull();
    expect(h.el.getSrc()).toBeNull();
  });
});

describe("AudioSession correction", () => {
  it("places the element at the film target on a tick", () => {
    const h = session({ "ps.snd.dest": "device" });
    h.s.onStatus(status({ timecode: { tc: 100, at: 1000, rate: 1, epoch: 1 } }));
    h.el.currentTime = 500; // wrong place
    h.s.correct(1000);
    expect(h.el.currentTime).toBeCloseTo(100, 3); // seeked to the film position
  });

  it("pauses the element when the daemon says the timeline is frozen", () => {
    const h = session({ "ps.snd.dest": "device" });
    h.s.onStatus(status({ state: "paused", timecode: { tc: 100, at: 1000, rate: 0, epoch: 1 } }));
    h.el.paused = false;
    h.s.correct(1000);
    expect(h.el.paused).toBe(true);
  });
});

describe("AudioSession track and trim", () => {
  it("clamps the trim to +/- 1 s", () => {
    const h = session({ "ps.snd.dest": "device" });
    for (let i = 0; i < 60; i++) {
      h.s.nudgeTrim(25);
    }
    expect(h.s.trim).toBe(1000);
  });

  it("switches destination to device when a language is tapped", async () => {
    const h = session();
    expect(h.s.listening).toBe(false);
    h.s.onStatus(status({ timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 } }));
    h.s.setTrack(TRACKS[1]!);
    await flush();
    expect(h.s.listening).toBe(true);
    expect(h.s.currentTrack).toBe(1);
  });
});
