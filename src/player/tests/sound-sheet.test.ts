import { describe, it, expect } from "vitest";
import { SoundSheet } from "../src/sound-sheet";
import { AudioSession, type AudioElement, type StorageLike } from "../src/audio";
import { ServerClock } from "../src/clock";
import { FilmClock } from "../src/timecode";
import { SyncController } from "../src/sync";
import { Tunables } from "../src/tunables";
import { CLOCK, SYNC, TIMING } from "../src/config";
import { makePage } from "./harness/page";
import { asDocument, FakeEl } from "./harness/dom";
import type { Status, Track } from "../src/types";

class FakeAudio implements AudioElement {
  currentTime = 0;
  duration = 7200;
  playbackRate = 1;
  paused = true;
  private src: string | null = null;
  play() {
    this.paused = false;
    return Promise.resolve();
  }
  pause() {
    this.paused = true;
  }
  load() {}
  getSrc() {
    return this.src;
  }
  setSrc(s: string) {
    this.src = s;
  }
  clearSrc() {
    this.src = null;
  }
}

class Mem implements StorageLike {
  d: Record<string, string> = {};
  getItem(k: string) {
    return k in this.d ? this.d[k]! : null;
  }
  setItem(k: string, v: string) {
    this.d[k] = v;
  }
  removeItem(k: string) {
    delete this.d[k];
  }
}

const TRACKS: Track[] = [
  { n: 0, lang: "eng", default: true, channels: 6 },
  { n: 1, lang: "fin" },
];

function harness(debug = false, langs: string[] = []) {
  const doc = makePage();
  const store = new Mem();
  const clock = new ServerClock(CLOCK);
  clock.adoptSession("s1");
  clock.accept(0, 0.003, 1000);
  const audio = new AudioSession({
    el: new FakeAudio(),
    clock,
    film: new FilmClock(clock),
    sync: new SyncController({ ...SYNC }),
    stamp: (p) => p,
    storage: store,
    now: () => 1000,
  });
  const tunables = new Tunables({ ...SYNC }, { ...CLOCK }, { ...TIMING });
  const sheet = new SoundSheet(asDocument(doc), audio, tunables, {
    debug,
    libraryLangs: () => langs,
    storage: store,
  });
  return { doc, audio, tunables, sheet, store };
}

const el = (doc: ReturnType<typeof makePage>, id: string) =>
  doc.getElementById(id) as unknown as FakeEl;

describe("SoundSheet", () => {
  it("offers mute and this-device, reflecting the current destination", () => {
    const h = harness();
    h.sheet.paint();
    const dest = el(h.doc, "destList");
    expect(dest.children).toHaveLength(2);
    dest.children[1]!.click(); // This device
    expect(h.audio.listening).toBe(true);
  });

  it("lists a language preference before any film starts", () => {
    const h = harness(false, ["eng", "fin"]);
    h.sheet.paint();
    expect(el(h.doc, "langList").children).toHaveLength(2);
    expect(el(h.doc, "sheetNote").textContent).toMatch(/Pick one now/);
  });

  it("lists the film's tracks with channel sub-labels once playing", () => {
    const h = harness();
    h.audio.setDest("device");
    h.audio.onStatus({
      state: "playing",
      id: "f",
      tracks: TRACKS,
      timecode: { tc: 0, at: 1000, rate: 1, epoch: 1 },
    } as Status);
    h.sheet.paint();
    const langs = el(h.doc, "langList");
    expect(langs.children).toHaveLength(2);
    expect(langs.children[0]!.children[0]!.children[0]!.textContent).toBe("6 channels");
  });

  it("shows the trim and nudges it", () => {
    const h = harness();
    h.audio.setDest("device");
    h.sheet.paint();
    el(h.doc, "syncFwd").click();
    h.sheet.paint();
    expect(el(h.doc, "syncVal").textContent).toBe("+25 ms");
  });

  it("hides the tune list unless ?debug is on", () => {
    const plain = harness(false);
    plain.sheet.paint();
    expect(el(plain.doc, "tuneList").classList.contains("on")).toBe(false);

    const debug = harness(true);
    debug.sheet.paint();
    expect(el(debug.doc, "tuneList").classList.contains("on")).toBe(true);
    expect(el(debug.doc, "tuneList").children.length).toBeGreaterThan(0);
  });

  it("saves a tuned value through the sound sheet", () => {
    const h = harness(true);
    h.sheet.paint();
    // The + button of the first tunable row.
    const firstRow = el(h.doc, "tuneList").children[0]!;
    firstRow.children[3]!.click(); // increase
    expect(h.store.d["ps.snd.tune"]).toBeDefined();
  });
});
