// Tilting the phone as a d-pad. Pure: orientation readings in, a button mask
// out, so every threshold here is tested without a phone.
//
// WHICH WAY IS "LEFT", WHATEVER WAY THE PHONE IS HELD
//
// The phone is held sideways in both hands, screen towards the face, at
// whatever angle is comfortable. So the mapping cannot be "beta means up":
// which device axis is left-right depends on which way round it is turned, and
// how far it leans back changes what beta and gamma mean.
//
// Instead the reading is turned into the direction of UP, the sky, as seen in
// the screen's own frame -- x to the right of the screen, y to its top, z out
// of the glass. That only needs beta and gamma (never alpha, the compass, which
// drifts), and the screen's rotation, which the browser reports. Then:
//
//   roll   how far the sky leans towards the screen's right. Lowering the left
//          hand rolls the phone anticlockwise, which leans the sky right, so a
//          positive change is LEFT -- like steering.
//   pitch  how far the sky leans towards the screen's top. Tipping the top
//          edge away from you leans the sky towards the bottom, so a negative
//          change is UP -- like pushing a joystick forward -- and tilting the
//          phone back towards you is DOWN.
//
// Both are measured from a baseline: how the phone was held when the game
// started, or when somebody tapped re-centre. Holding it flat, or upright, or
// lying on a sofa all start as "no direction".
//
// HYSTERESIS
//
// A direction presses at PRESS_DEG past the baseline and lets go only below
// RELEASE_DEG. A hand that rests at exactly the threshold would otherwise
// flicker the button on and off every reading, which in a game is a character
// that stutters.

export const UP = 1;
export const DOWN = 2;
export const LEFT = 4;
export const RIGHT = 8;
export const A = 16;
export const B = 32;
export const START = 64;
export const SELECT = 128;
export const DPAD = UP | DOWN | LEFT | RIGHT;

export const PRESS_DEG = 12;
export const RELEASE_DEG = 8;

/** One DeviceOrientationEvent's beta and gamma, in degrees. */
export interface Tilt {
  beta: number;
  gamma: number;
}

const RAD = Math.PI / 180;

/** The sky's direction in the screen's frame, for a reading taken with the
 * screen rotated `angle` degrees (screen.orientation.angle). */
export function skyInScreen(t: Tilt, angle: number): [number, number, number] {
  const b = t.beta * RAD;
  const g = t.gamma * RAD;
  // In the device's own frame (x across the short edge, y up the long one,
  // z out of the glass), from the W3C rotation order Z-X'-Y''.
  const ux = -Math.sin(g) * Math.cos(b);
  const uy = Math.sin(b);
  const uz = Math.cos(b) * Math.cos(g);
  // Turned into the screen's frame. At 90 the device's top points left, so
  // the screen's right is the device's -y and its top the device's +x.
  const a = angle * RAD;
  const sx = ux * Math.cos(a) - uy * Math.sin(a);
  const sy = ux * Math.sin(a) + uy * Math.cos(a);
  return [sx, sy, uz];
}

/** Roll and pitch, in degrees, of one reading. */
export function rollPitch(t: Tilt, angle: number): { roll: number; pitch: number } {
  const [sx, sy, sz] = skyInScreen(t, angle);
  return {
    roll: Math.atan2(sx, Math.hypot(sy, sz)) / RAD,
    pitch: Math.atan2(sy, sz) / RAD,
  };
}

/** Wrap a difference of angles into -180..180, so a baseline near the seam
 * does not read as a turn of a whole circle. */
function wrap(deg: number): number {
  return ((((deg + 180) % 360) + 360) % 360) - 180;
}

export class TiltPad {
  private base: { roll: number; pitch: number } | null = null;
  private mask = 0;
  /** The last reading, so re-centre can use it without waiting for another. */
  private last: { roll: number; pitch: number } | null = null;

  /** The directions currently held, as UP | DOWN | LEFT | RIGHT bits. */
  get held(): number {
    return this.mask;
  }

  /** Take the next reading as "no direction". */
  recenter(): void {
    this.base = this.last;
    this.mask = 0;
  }

  /** Forget everything: tilt off, or the phone put down. */
  reset(): void {
    this.base = null;
    this.last = null;
    this.mask = 0;
  }

  /** One reading in; the directions now held out. */
  update(t: Tilt, angle: number): number {
    const now = rollPitch(t, angle);
    this.last = now;
    if (this.base === null) {
      // The first reading after starting is the baseline.
      this.base = now;
      return this.mask;
    }
    const roll = wrap(now.roll - this.base.roll);
    const pitch = wrap(now.pitch - this.base.pitch);
    this.mask =
      this.axis(roll, LEFT, RIGHT) | this.axis(-pitch, UP, DOWN);
    return this.mask;
  }

  private axis(delta: number, pos: number, neg: number): number {
    for (const [bit, value] of [
      [pos, delta],
      [neg, -delta],
    ] as const) {
      const held = (this.mask & bit) !== 0;
      if (value >= PRESS_DEG || (held && value >= RELEASE_DEG)) {
        return bit;
      }
    }
    return 0;
  }
}
