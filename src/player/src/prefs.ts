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

const SHELF_KEY = "playstick.shelf";

/** The shelf this phone last looked at: "games" or "films". */
export function loadShelf(storage: StorageLike | null): "films" | "games" {
  try {
    return storage?.getItem(SHELF_KEY) === "games" ? "games" : "films";
  } catch {
    return "films";
  }
}

export function saveShelf(storage: StorageLike | null, shelf: "films" | "games"): void {
  try {
    storage?.setItem(SHELF_KEY, shelf);
  } catch {
    // Not remembered this time.
  }
}

const PHONE_KEY = "playstick.phone";
const PROFILE_KEY = "playstick.profile";

/**
 * This phone's id for its calibration profile, made up once and kept.
 *
 * Random rather than derived from anything about the phone: it names a phone
 * to the stick and nothing else. If storage is cleared it is lost, and the
 * calibration sheet offers the stick's saved profiles to pick this phone's
 * again from.
 */
export function loadPhoneId(storage: StorageLike | null, make: () => string): string {
  try {
    const have = storage?.getItem(PHONE_KEY);
    if (have && /^[A-Za-z0-9-]{8,64}$/.test(have)) {
      return have;
    }
  } catch {
    // Fall through and make one; it lasts this page load at least.
  }
  const id = make();
  savePhoneId(storage, id);
  return id;
}

export function savePhoneId(storage: StorageLike | null, id: string): void {
  try {
    storage?.setItem(PHONE_KEY, id);
  } catch {
    // Not remembered this time.
  }
}

/** The last profile this phone saved or fetched, as JSON, or null. Parsed and
 * checked by calibration.ts, which knows the shape. */
export function loadProfileJson(storage: StorageLike | null): unknown {
  try {
    return JSON.parse(storage?.getItem(PROFILE_KEY) || "null");
  } catch {
    return null;
  }
}

export function saveProfileJson(storage: StorageLike | null, profile: unknown): void {
  try {
    storage?.setItem(PROFILE_KEY, JSON.stringify(profile));
  } catch {
    // Not remembered this time; the stick still has it.
  }
}
