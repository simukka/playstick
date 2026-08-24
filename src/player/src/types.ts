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
}

export interface Projector {
  model: string;
  power: string;
  fault: string;
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
  thumbs_pending?: number;
  prepare?: { label?: string } | null;
  notice?: string;
  timecode?: Timecode | null;
  projector?: Projector;
}

export interface LibraryReply {
  items: LibraryItem[];
  available?: boolean;
}

export interface TimeReply {
  now: number;
  session: string;
}
