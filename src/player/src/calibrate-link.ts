// The phone's end of /api/calibrate/socket: what the wizard is doing, and the
// phone's raw readings, up to the projector (src/server/playstick/calibrate.py).
//
// Both are newest-only. The wizard may change its scene on every orientation
// event -- sixty a second on an iPhone -- and the projector redraws at most
// twenty-five times a second anyway, so the link holds the latest of each and
// sends whatever is newest every TICK_MS. A scene that has not changed is not
// sent again; telemetry is sent every tick while there is some, which is also
// the heartbeat that tells the stick a phone is still holding the wizard.
//
// A socket that drops is reopened after RETRY_MS while the wizard is open, and
// the current scene is sent again first, so the projector picks up where the
// phone is rather than where it was.
import type { SocketLike } from "./game-link";

export const TICK_MS = 40;
export const RETRY_MS = 1000;

/** What the projector draws, in the vocabulary calibrate.py accepts. */
export interface Scene {
  step: "intro" | "sweep" | "targets" | "tilt" | "buttons" | "summary";
  name?: string;
  edge?: "left" | "right" | "top" | "bottom" | null;
  button?: "A" | "B" | "START" | "SELECT" | null;
  hold?: number;
  targets?: { x: number; y: number; r: number; state: "pending" | "active" | "hit" }[];
  lights?: number;
  summary?: Partial<Record<"yaw_across" | "pitch_across" | "error" | "press" | "jitter", number>>;
}

/** One reading, as the projector's panel shows it. */
export interface Telemetry {
  a: number | null;
  b: number | null;
  g: number | null;
  angle: number;
  touch: [number, number] | null;
  mask: number;
  dot: [number, number] | null;
}

export interface CalibrateLinkDeps {
  open: (url: string) => SocketLike;
  url: string;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  every: (fn: () => void, ms: number) => number;
  stopEvery: (id: number) => void;
}

const OPEN = 1;

export class CalibrateLink {
  private sock: SocketLike | null = null;
  private wanted = false;
  private retry = 0;
  private beat = 0;
  private scene: Scene | null = null;
  private sceneSent = "";
  private tel: Telemetry | null = null;
  sent = 0;

  constructor(private readonly deps: CalibrateLinkDeps) {}

  get connected(): boolean {
    return this.sock !== null && this.sock.readyState === OPEN;
  }

  /** Keep a socket open until close(). Idempotent. */
  open(): void {
    if (this.wanted) {
      return;
    }
    this.wanted = true;
    this.connect();
    this.beat = this.deps.every(() => this.flush(), TICK_MS);
  }

  close(): void {
    this.wanted = false;
    this.deps.stopEvery(this.beat);
    if (this.retry) {
      this.deps.cancel(this.retry);
      this.retry = 0;
    }
    const sock = this.sock;
    this.sock = null;
    this.sceneSent = "";
    this.tel = null;
    if (sock) {
      sock.onclose = null;
      sock.close();
    }
  }

  setScene(scene: Scene): void {
    this.scene = scene;
  }

  setTel(tel: Telemetry): void {
    this.tel = tel;
  }

  /** The wizard is over: the stick takes the black screen away. */
  done(): void {
    if (this.connected) {
      this.sock!.send(JSON.stringify({ done: true }));
    }
  }

  /** Send whatever is newest. Called every TICK_MS; exposed for the tests. */
  flush(): void {
    if (!this.connected) {
      return;
    }
    const message: { scene?: Scene; tel?: Telemetry } = {};
    if (this.scene) {
      const text = JSON.stringify(this.scene);
      if (text !== this.sceneSent) {
        message.scene = this.scene;
        this.sceneSent = text;
      }
    }
    if (this.tel) {
      message.tel = this.tel;
    }
    if (message.scene || message.tel) {
      this.sock!.send(JSON.stringify(message));
      this.sent++;
    }
  }

  private connect(): void {
    const sock = this.deps.open(this.deps.url);
    this.sock = sock;
    sock.onopen = () => {
      this.sceneSent = "";
      this.flush();
    };
    sock.onmessage = null;
    sock.onclose = () => {
      if (this.sock !== sock) {
        return;
      }
      this.sock = null;
      if (this.wanted) {
        this.retry = this.deps.schedule(() => {
          this.retry = 0;
          if (this.wanted) {
            this.connect();
          }
        }, RETRY_MS);
      }
    };
  }
}

/** ws:// or wss://, matching how the page itself was loaded. */
export function calibrateUrl(loc: { protocol: string; host: string }): string {
  return (loc.protocol === "https:" ? "wss://" : "ws://") + loc.host + "/api/calibrate/socket";
}
