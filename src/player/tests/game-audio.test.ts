// The game sound's buffer: that it waits to fill, plays what it was given,
// steers its rate towards the target, and cuts a backlog rather than playing
// the past.
import { describe, it, expect } from "vitest";
import { GameAudio, JitterBuffer, MAX_S, TARGET_S } from "../src/game-audio";

function pcm(samples: number[]): ArrayBuffer {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  samples.forEach((v, i) => view.setInt16(i * 2, v, true));
  return buf;
}

const RATE = 24000;
const constant = (n: number, v = 16384) => pcm(new Array(Math.round(n)).fill(v));

describe("JitterBuffer", () => {
  it("is silent until it holds the target", () => {
    const b = new JitterBuffer(RATE);
    b.push(constant(Math.floor(RATE * TARGET_S) - 10));
    const out = new Float32Array(64);
    b.pull(out, RATE);
    expect([...out].every((v) => v === 0)).toBe(true);
  });

  it("plays what it was given once primed", () => {
    const b = new JitterBuffer(RATE);
    b.push(constant(RATE * 0.1));
    const out = new Float32Array(64);
    b.pull(out, RATE);
    expect(out[10]).toBeCloseTo(0.5, 3);
  });

  it("goes silent and counts it when it runs dry", () => {
    const b = new JitterBuffer(RATE);
    b.push(constant(RATE * 0.07));
    const out = new Float32Array(Math.round(RATE * 0.1));
    b.pull(out, RATE);
    expect(out[out.length - 1]).toBe(0);
    expect(b.underruns).toBe(1);
  });

  it("cuts a backlog back to the target", () => {
    const b = new JitterBuffer(RATE);
    b.push(constant(Math.ceil(RATE * (MAX_S + 0.05))));
    expect(b.held).toBeCloseTo(TARGET_S, 2);
    expect(b.trimmed).toBe(1);
  });

  it("plays a little faster when it holds too much", () => {
    // Twice the target held: a pull of a fixed length consumes more than
    // one sample per output sample.
    const full = new JitterBuffer(RATE);
    full.push(constant(RATE * TARGET_S * 2));
    const before = full.held;
    full.pull(new Float32Array(1000), RATE);
    expect(before - full.held).toBeGreaterThan(1000 / RATE);
  });

  it("resamples to a different device rate", () => {
    const b = new JitterBuffer(RATE);
    b.push(constant(RATE * 0.2));
    const before = b.held;
    b.pull(new Float32Array(4800), 48000);
    // 4800 samples at 48 kHz is 0.1 s, which is 2400 of ours, within the skew.
    expect(before - b.held).toBeCloseTo(0.1, 2);
  });
});

describe("GameAudio", () => {
  it("asks iOS for the playback session, so the mute switch leaves it alone", () => {
    const session = { type: "auto" };
    const audio = new GameAudio({ makeContext: () => null, session });
    audio.unlock();
    expect(session.type).toBe("playback");
  });

  it("does nothing without an audio context", () => {
    const audio = new GameAudio({ makeContext: () => null });
    audio.unlock();
    audio.start(RATE);
    expect(audio.on).toBe(false);
  });
});
