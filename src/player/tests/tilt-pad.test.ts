// The tilt d-pad: which way is which with the phone held sideways, and the
// hysteresis that keeps a resting hand from flickering a direction.
import { describe, it, expect } from "vitest";
import {
  DOWN,
  LEFT,
  PRESS_DEG,
  RELEASE_DEG,
  RIGHT,
  TILT_CALIBRATION_DEFAULT,
  TiltPad,
  UP,
  rollPitch,
} from "../src/tilt-pad";

// Held sideways with the phone's top to the left (screen.orientation.angle
// 90), leaning back 40 degrees from upright. In that pose beta is the
// left-right tilt and gamma the forward-back one.
const LANDSCAPE = 90;
const rest = { beta: 0, gamma: -50 };

function padAt(): TiltPad {
  const pad = new TiltPad();
  pad.update(rest, LANDSCAPE); // the first reading is the baseline
  return pad;
}

describe("TiltPad directions", () => {
  it("is still at the pose it started in", () => {
    expect(padAt().update(rest, LANDSCAPE)).toBe(0);
  });

  it("lowering the left hand is LEFT and the right hand is RIGHT", () => {
    // The phone's top is at the left, so lowering the left hand lowers the
    // top: beta goes negative.
    expect(padAt().update({ beta: -20, gamma: -50 }, LANDSCAPE)).toBe(LEFT);
    expect(padAt().update({ beta: 20, gamma: -50 }, LANDSCAPE)).toBe(RIGHT);
  });

  it("tipping the far edge away is UP and back towards you is DOWN", () => {
    // The screen's top is the phone's right edge here. Tipping it away
    // lowers it, which is gamma rising towards zero from -50.
    expect(padAt().update({ beta: 0, gamma: -30 }, LANDSCAPE)).toBe(UP);
    expect(padAt().update({ beta: 0, gamma: -70 }, LANDSCAPE)).toBe(DOWN);
  });

  it("allows a diagonal", () => {
    expect(padAt().update({ beta: -20, gamma: -30 }, LANDSCAPE)).toBe(LEFT | UP);
  });

  it("means the same with the phone turned the other way round", () => {
    const pad = new TiltPad();
    const flipped = { beta: 0, gamma: 50 };
    pad.update(flipped, 270);
    // At 270 the phone's top is at the right, so lowering the left hand
    // raises the top: beta goes positive.
    expect(pad.update({ beta: 20, gamma: 50 }, 270)).toBe(LEFT);
  });

  it("reads roll and pitch as zero flat on a table in portrait", () => {
    const rp = rollPitch({ beta: 0, gamma: 0 }, 0);
    expect(Math.abs(rp.roll)).toBeLessThan(1e-9);
    expect(Math.abs(rp.pitch)).toBeLessThan(1e-9);
  });
});

describe("TiltPad hysteresis", () => {
  const roll = (deg: number) => ({ beta: -deg, gamma: -50 });

  it("presses at the press angle and lets go only below the release angle", () => {
    const pad = padAt();
    expect(pad.update(roll(PRESS_DEG - 1), LANDSCAPE)).toBe(0);
    expect(pad.update(roll(PRESS_DEG + 1), LANDSCAPE)).toBe(LEFT);
    // Between the two: still held.
    expect(pad.update(roll((PRESS_DEG + RELEASE_DEG) / 2), LANDSCAPE)).toBe(LEFT);
    expect(pad.update(roll(RELEASE_DEG - 1), LANDSCAPE)).toBe(0);
  });

  it("re-centre makes the way it is held now the level", () => {
    const pad = padAt();
    expect(pad.update(roll(30), LANDSCAPE)).toBe(LEFT);
    pad.recenter();
    expect(pad.held).toBe(0);
    expect(pad.update(roll(30), LANDSCAPE)).toBe(0);
    expect(pad.update(roll(10), LANDSCAPE)).toBe(RIGHT);
  });
});

describe("TiltPad calibration", () => {
  const roll = (deg: number) => ({ beta: -deg, gamma: -50 });
  const padWith = (cal: typeof TILT_CALIBRATION_DEFAULT) => {
    const pad = new TiltPad(cal);
    pad.update(rest, LANDSCAPE);
    return pad;
  };

  it("presses each direction at its own angle", () => {
    const pad = padWith({ ...TILT_CALIBRATION_DEFAULT, press: { left: 6, right: 20, up: 12, down: 12 } });
    expect(pad.update(roll(7), LANDSCAPE)).toBe(LEFT);
    expect(pad.update(roll(-15), LANDSCAPE)).toBe(0);
    expect(pad.update(roll(-21), LANDSCAPE)).toBe(RIGHT);
  });

  it("lets go at the release share of the press", () => {
    const pad = padWith({ ...TILT_CALIBRATION_DEFAULT, release_ratio: 0.5 });
    pad.update(roll(13), LANDSCAPE);
    expect(pad.update(roll(7), LANDSCAPE)).toBe(LEFT);
    expect(pad.update(roll(5), LANDSCAPE)).toBe(0);
  });

  it("swaps left and right for a flipped roll", () => {
    const pad = padWith({ ...TILT_CALIBRATION_DEFAULT, roll_sign: -1 });
    expect(pad.update(roll(20), LANDSCAPE)).toBe(RIGHT);
  });

  it("reports how far from level it is", () => {
    const pad = padWith(TILT_CALIBRATION_DEFAULT);
    pad.update(roll(20), LANDSCAPE);
    expect(pad.offset!.roll).toBeCloseTo(20, 0);
  });
});
