// The wizard's arithmetic: holds, spans, press points, the button picker, and
// reading a profile back from the stick.
import { describe, it, expect } from "vitest";
import {
  ButtonPicker,
  DEFAULT_PROFILE,
  TiltCapture,
  fitPointer,
  inside,
  makePhoneId,
  miss,
  parseProfile,
  pressFor,
  profileBody,
  summarise,
  swapAB,
  type Hold,
} from "../src/calibration";
import { BUTTONS_DEFAULT } from "../src/gamepad";
import { DEGREES_ACROSS, SCREEN_ASPECT, SMOOTHING, POINTER_CALIBRATION_DEFAULT } from "../src/pointer";
import { PRESS_DEG, RELEASE_DEG, TILT_CALIBRATION_DEFAULT } from "../src/tilt-pad";

const hold = (alpha: number, beta: number, jitter = 0): Hold => ({ mean: { alpha, beta }, jitter });

describe("the defaults", () => {
  it("are the constants the pointer and the pad always had", () => {
    expect(POINTER_CALIBRATION_DEFAULT.yaw_across).toBe(DEGREES_ACROSS);
    expect(POINTER_CALIBRATION_DEFAULT.pitch_across).toBeCloseTo(DEGREES_ACROSS / SCREEN_ASPECT);
    expect(POINTER_CALIBRATION_DEFAULT.smoothing).toBe(SMOOTHING);
    expect(TILT_CALIBRATION_DEFAULT.press.left).toBe(PRESS_DEG);
    expect(TILT_CALIBRATION_DEFAULT.press.left * TILT_CALIBRATION_DEFAULT.release_ratio).toBeCloseTo(
      RELEASE_DEG,
    );
  });
});

describe("summarise", () => {
  it("averages a hold and says how much it wobbled", () => {
    const h = summarise([
      { alpha: 10, beta: 1 },
      { alpha: 12, beta: 3 },
    ])!;
    expect(h.mean.alpha).toBeCloseTo(11);
    expect(h.mean.beta).toBeCloseTo(2);
    expect(h.jitter).toBeCloseTo(1);
  });

  it("averages a hold across north to north, not south", () => {
    const h = summarise([
      { alpha: 358, beta: 0 },
      { alpha: 2, beta: 0 },
    ])!;
    expect(Math.min(h.mean.alpha, 360 - h.mean.alpha)).toBeCloseTo(0);
  });

  it("is nothing for no readings", () => {
    expect(summarise([])).toBeNull();
  });
});

describe("fitPointer", () => {
  it("measures the spans, the signs and the middle", () => {
    const fit = fitPointer({
      left: hold(30, 0, 0.2),
      right: hold(350, 0, 0.2),
      top: hold(10, 12, 0.2),
      bottom: hold(10, -12, 0.2),
    });
    if ("error" in fit) throw new Error(fit.error);
    expect(fit.cal.yaw_across).toBe(40);
    expect(fit.cal.pitch_across).toBe(24);
    expect(fit.cal.yaw_sign).toBe(1);
    expect(fit.cal.pitch_sign).toBe(1);
    expect(fit.centre.alpha).toBeCloseTo(10);
    expect(fit.centre.beta).toBeCloseTo(0);
    expect(fit.jitter).toBeCloseTo(0.2);
  });

  it("flips an axis that read the other way", () => {
    const fit = fitPointer({
      left: hold(350, 0),
      right: hold(30, 0),
      top: hold(10, -12),
      bottom: hold(10, 12),
    });
    if ("error" in fit) throw new Error(fit.error);
    expect(fit.cal.yaw_sign).toBe(-1);
    expect(fit.cal.pitch_sign).toBe(-1);
  });

  it("calms the dot for a shaky hand", () => {
    const steady = fitPointer({ left: hold(30, 0), right: hold(350, 0), top: hold(10, 12), bottom: hold(10, -12) });
    const shaky = fitPointer({
      left: hold(30, 0, 1),
      right: hold(350, 0, 1),
      top: hold(10, 12, 1),
      bottom: hold(10, -12, 1),
    });
    if ("error" in steady || "error" in shaky) throw new Error("no fit");
    expect(shaky.cal.smoothing).toBeLessThan(steady.cal.smoothing);
    expect(shaky.cal.deadzone).toBeGreaterThan(steady.cal.deadzone);
  });

  it("says so when the phone barely moved", () => {
    const fit = fitPointer({ left: hold(10, 0), right: hold(8, 0), top: hold(10, 12), bottom: hold(10, -12) });
    expect("error" in fit).toBe(true);
    const flat = fitPointer({ left: hold(30, 0), right: hold(350, 0), top: hold(10, 1), bottom: hold(10, -1) });
    expect("error" in flat).toBe(true);
  });
});

describe("targets", () => {
  it("counts a dot inside the circle, scaled for the 16:9 picture", () => {
    const t = { x: 0.5, y: 0.5, r: 0.06 };
    expect(inside({ x: 0.5, y: 0.55 }, t)).toBe(true);
    // 0.04 of the width is 0.071 of the height: outside.
    expect(inside({ x: 0.54, y: 0.5 }, t)).toBe(false);
    expect(miss({ x: 0.54, y: 0.5 }, t)).toBeCloseTo(0.04);
  });
});

describe("TiltCapture", () => {
  it("takes the peak once the tilt has come back", () => {
    const c = new TiltCapture("left");
    expect(c.update({ roll: 10, pitch: 0 })).toBeNull();
    expect(c.update({ roll: 20, pitch: 0 })).toBeNull();
    expect(c.update({ roll: 2, pitch: 0 })).toEqual({ peak: 20, flipped: false });
  });

  it("notices a tilt the other way", () => {
    const c = new TiltCapture("up");
    c.update({ roll: 0, pitch: 15 });
    expect(c.update({ roll: 0, pitch: 0 })).toEqual({ peak: 15, flipped: true });
  });

  it("does not count a wobble", () => {
    const c = new TiltCapture("right");
    c.update({ roll: -5, pitch: 0 });
    expect(c.update({ roll: 0, pitch: 0 })).toBeNull();
  });

  it("presses at a share of the peak, within bounds", () => {
    expect(pressFor(20)).toBe(12);
    expect(pressFor(2)).toBe(6);
    expect(pressFor(90)).toBe(30);
  });
});

describe("ButtonPicker", () => {
  it("hands out A, B, START, SELECT in order", () => {
    const p = new ButtonPicker();
    expect(p.wanted).toBe("A");
    p.tap("B");
    p.tap("A");
    p.tap("START");
    expect(p.tap("B")).toEqual({ already: "A" });
    p.tap("SELECT");
    expect(p.done).toBe(true);
    expect(p.map()).toEqual({ B: BUTTONS_DEFAULT.A, A: BUTTONS_DEFAULT.B, START: 64, SELECT: 128 });
  });

  it("swaps A and B", () => {
    expect(swapAB(BUTTONS_DEFAULT)).toEqual({ A: 32, B: 16, START: 64, SELECT: 128 });
  });
});

describe("parseProfile", () => {
  const good = { ...profileBody({ ...DEFAULT_PROFILE, name: "Kid" }), id: "phone-1234abcd", updated_at: 5 };

  it("reads back a profile the stick stored", () => {
    const p = parseProfile(JSON.parse(JSON.stringify(good)))!;
    expect(p.name).toBe("Kid");
    expect(p.id).toBe("phone-1234abcd");
    expect(p.pointer.yaw_across).toBe(DEGREES_ACROSS);
  });

  it("is all or nothing", () => {
    expect(parseProfile(null)).toBeNull();
    expect(parseProfile({ ...good, pointer: { ...good.pointer, yaw_across: 500 } })).toBeNull();
    expect(parseProfile({ ...good, tilt: { ...good.tilt, roll_sign: 0 } })).toBeNull();
    expect(parseProfile({ ...good, buttons: { A: 16, B: 16, START: 64, SELECT: 128 } })).toBeNull();
    expect(parseProfile({ ...good, buttons: { ...good.buttons, A: 1 } })).toBeNull();
  });
});

describe("makePhoneId", () => {
  it("is an id the stick will take", () => {
    expect(makePhoneId()).toMatch(/^[A-Za-z0-9-]{8,64}$/);
  });
});
