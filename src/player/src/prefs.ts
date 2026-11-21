// Per-phone settings, in localStorage, for things only this phone cares about:
// how fast this person likes the pointer, and in what colour.
//
// Every read and write is wrapped. Storage throws in a private window, when site
// data is blocked, and in some embedded browsers, and a pointer that cannot
// remember its speed is a pointer -- one that will not open is not.

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface PointerPrefs {
  /** 0.5 .. 3: screen widths per pad width, and the inverse for tilting. */
  speed: number;
  size: "s" | "m" | "l";
  color: "red" | "green" | "white";
  /** Aim by tilting the phone, where the browser allows it. */
  tilt: boolean;
}

export const POINTER_DEFAULTS: PointerPrefs = {
  speed: 1.5,
  size: "m",
  color: "red",
  tilt: true,
};

const KEY = "playstick.pointer";

export function loadPointerPrefs(storage: StorageLike | null): PointerPrefs {
  let raw: unknown = null;
  try {
    raw = JSON.parse(storage?.getItem(KEY) || "null");
  } catch {
    raw = null;
  }
  const got = (raw && typeof raw === "object" ? raw : {}) as Partial<PointerPrefs>;
  const speed = Number(got.speed);
  return {
    speed: Number.isFinite(speed) ? Math.min(3, Math.max(0.5, speed)) : POINTER_DEFAULTS.speed,
    size: got.size === "s" || got.size === "l" ? got.size : "m",
    color: got.color === "green" || got.color === "white" ? got.color : "red",
    tilt: got.tilt === undefined ? POINTER_DEFAULTS.tilt : !!got.tilt,
  };
}

export function savePointerPrefs(storage: StorageLike | null, prefs: PointerPrefs): void {
  try {
    storage?.setItem(KEY, JSON.stringify(prefs));
  } catch {
    // Not remembered this time. See the top of the file.
  }
}

/** window.localStorage, or null where even asking for it throws. */
export function browserStorage(): StorageLike | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
