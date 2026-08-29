// The daemon, as a typed client. Every route is same-origin, so nothing here
// deals with hosts or auth. The one subtlety is that a control POST reports both
// the HTTP ok flag and the parsed body, because the daemon answers a refusal
// (a film already playing) with 4xx AND a JSON reason the page shows verbatim.
import type { LibraryReply, Status, TimeReply } from "./types";

export interface PostResult {
  ok: boolean;
  data: { error?: string } & Record<string, unknown>;
}

type FetchFn = (input: string, init?: RequestInit) => Promise<{
  ok: boolean;
  json(): Promise<unknown>;
}>;

export class ApiClient {
  constructor(private readonly fetchFn: FetchFn) {}

  async status(header?: string): Promise<Status> {
    // Same-origin, so a custom header costs no preflight. It carries the sync
    // telemetry when ?debug is on and is absent otherwise.
    const init = header
      ? { headers: { "X-Playstick-Sync": header } }
      : undefined;
    const r = await this.fetchFn("/api/status", init);
    return (await r.json()) as Status;
  }

  async library(): Promise<LibraryReply> {
    const r = await this.fetchFn("/api/library");
    return (await r.json()) as LibraryReply;
  }

  async time(): Promise<TimeReply> {
    const r = await this.fetchFn("/api/time");
    return (await r.json()) as TimeReply;
  }

  async post(path: string, body?: unknown): Promise<PostResult> {
    const r = await this.fetchFn(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    const data = (await r.json()) as PostResult["data"];
    return { ok: r.ok, data };
  }

  play(id: string): Promise<PostResult> {
    return this.post("/api/play", { id });
  }
  pause(): Promise<PostResult> {
    return this.post("/api/pause");
  }
  resume(): Promise<PostResult> {
    return this.post("/api/resume");
  }
  stop(): Promise<PostResult> {
    return this.post("/api/stop");
  }
  volume(delta: number): Promise<PostResult> {
    return this.post("/api/volume", { delta });
  }
  /** Seconds, signed. One request per gesture, not one per press -- see
   * SeekPad. The daemon clamps to the film and answers with the position it
   * actually reached, so the bar does not have to guess. */
  seek(delta: number): Promise<PostResult> {
    return this.post("/api/seek", { delta });
  }
  /** Which subtitle track is on the picture, 0 for none. */
  subtitle(track: number): Promise<PostResult> {
    return this.post("/api/subtitle", { track });
  }
  /** Takes no body: there is one NAS and its address is a setting on the
   * daemon, not something this page may name. */
  wakeNas(): Promise<PostResult> {
    return this.post("/api/nas/wake");
  }
}
