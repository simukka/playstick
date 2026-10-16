// The shapes the daemon speaks. These mirror what http.py serialises; nothing
// here is inferred loosely, because the whole point of the rewrite is that a
// field the server stopped sending becomes a type error rather than a silent
// `undefined` three layers into the controller.

export interface Timecode {
  /** Film position, in seconds, at the instant `at`. */
  tc: number;
  /** The daemon-clock instant `tc` was true. */
  at: number;
  /** 0 when the timeline is not advancing (paused, buffering); else 1. */
  rate: number;
  /** Timeline identity. A change means a seek/reset, not a drift. */
  epoch: number;
}

export interface Track {
  n: number;
  lang: string;
  title?: string;
  channels?: number;
  default?: boolean;
  /** The extracted track's own origin against the film clock, in seconds. */
  offset?: number;
}

export interface LibraryItem {
  id: string;
  title: string;
  sort_title?: string;
  year?: number | string | null;
  rating?: number | string | null;
  genres?: string[];
  audio_langs?: string[];
  hidden?: boolean;
  has_thumb?: boolean;
  /** Set only on an episode: the series it belongs to. Every row sharing one
   * is folded into a single tile on the grid, and the rest are offered by the
   * episode sheet. Absent on a film, and on a daemon from before episodes. */
  series?: string | null;
  /** 0 for specials. Present whenever `series` is. */
  season?: number | null;
  episode?: number | null;
}

export interface Projector {
  model: string;
  power: string;
  fault: string;
}

/** The machine the films live on. Shaped like Projector deliberately: the page
 * should not need a second vocabulary for a second device. */
export interface Nas {
  /** "on" | "off" | "waking" | "unknown". "unknown" means no NAS is configured
   * -- or none has been probed yet -- and the page says nothing extra. */
  power: string;
  /** Whether this daemon could switch it on if asked. False when no MAC is
   * configured, so the page never offers an action that cannot work. */
  can_wake: boolean;
}

export type PlayerState =
  | "idle"
  | "preparing"
  | "playing"
  | "paused"
  | "airplay"
  | "unavailable";

export interface Status {
  state: PlayerState;
  build?: string;
  id?: string;
  title?: string;
  position?: number;
  position_valid?: boolean;
  buffering?: boolean;
  duration?: number;
  volume?: number | null;
  audio?: boolean;
  phone_audio?: boolean;
  tracks?: Track[];
  /** The subtitle tracks mpv loaded for this film, as mpv numbers them. Empty
   * when it has none -- a walked share with nothing prepped, or a film whose
   * only subtitles are the image-based kind prep declines to OCR. */
  subs?: Track[];
  /** Which of them is on the picture, 0 for none. Shared: subtitles are on the
   * projector everybody is looking at, unlike the sound, which is per phone. */
  sub?: number;
  thumbs_pending?: number;
  prepare?: { label?: string } | null;
  notice?: string;
  timecode?: Timecode | null;
  projector?: Projector;
  /** Absent on a daemon from before wake-on-LAN existed, which is why the
   * presenter falls back to the plain sentence rather than assuming. */
  nas?: Nas;
}

export interface LibraryReply {
  items: LibraryItem[];
  available?: boolean;
}

export interface TimeReply {
  now: number;
  session: string;
}
