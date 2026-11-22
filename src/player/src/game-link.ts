// The phone's end of /api/game/socket: buttons up, roles and sound down.
//
// The mask goes up as [seq u8, mask u16] whenever it changes, and again every
// HEARTBEAT_MS while the socket is open. The repeat is what lets the daemon
// tell a phone that is holding RIGHT from a phone that locked its screen while
// holding RIGHT: the second one goes quiet, and after a second the daemon lets
// go of its buttons (see game.py, STALE_SECONDS).
//
// A socket that drops is reopened after RETRY_MS for as long as the gamepad is
// up. On reconnecting, the phone asks for player one again only if it was
// player one, so a blip on the Wi-Fi does not hand the first pad to whoever
// happened to be listening.

export const HEARTBEAT_MS = 100;
export const RETRY_MS = 1000;

export type Role = "p1" | "p2" | "listener" | "";

export interface GameInfo {
  system?: string;
  players?: number;
  seats?: boolean[];
  listeners?: number;
  rate?: number;
}

/** What a browser WebSocket is to this class: injected, so tests drive it. */
export interface SocketLike {
  binaryType: string;
  readyState: number;
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  send(data: string | ArrayBuffer): void;
  close(): void;
}

export interface LinkDeps {
  open: (url: string) => SocketLike;
  url: string;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  every: (fn: () => void, ms: number) => number;
  stopEvery: (id: number) => void;
  onRole: (role: Role, game: GameInfo | null) => void;
  onSound: (pcm: ArrayBuffer) => void;
}

const OPEN = 1;

export class GameLink {
  private sock: SocketLike | null = null;
  private wanted = false;
  private retry = 0;
  private beat = 0;
  private seq = 0;
  private mask = 0;
  private _role: Role = "";
  /** Which seat to ask for when the socket (re)opens. */
  private want: "auto" | "any" | "" = "auto";

  constructor(private readonly deps: LinkDeps) {}

  get role(): Role {
    return this._role;
  }

  get connected(): boolean {
    return this.sock !== null && this.sock.readyState === OPEN;
  }

  /** Keep a socket open until close(). Idempotent. */
  open(): void {
    if (this.wanted) {
      return;
    }
    this.wanted = true;
    this.want = "auto";
    this.connect();
    this.beat = this.deps.every(() => this.send(), HEARTBEAT_MS);
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
    this.mask = 0;
    this._role = "";
    if (sock) {
      sock.onclose = null;
      sock.close();
    }
  }

  /** Ask for a seat: "any" takes whichever pad is free. */
  join(how: "auto" | "any" = "any"): void {
    this.want = how;
    this.sendText({ join: how });
  }

  leave(): void {
    this.want = "";
    this.sendText({ leave: true });
  }

  /** The buttons now held. Sent at once if it changed. */
  setMask(mask: number): void {
    if (mask === this.mask) {
      return;
    }
    this.mask = mask;
    this.send();
  }

  private connect(): void {
    const sock = this.deps.open(this.deps.url);
    sock.binaryType = "arraybuffer";
    this.sock = sock;
    sock.onopen = () => {
      if (this.want) {
        this.sendText({ join: this.want });
      }
      this.send();
    };
    sock.onmessage = (ev) => this.receive(ev.data);
    sock.onclose = () => {
      if (this.sock !== sock) {
        return;
      }
      this.sock = null;
      const was = this._role;
      this._role = "";
      // Only reclaim player one; any other seat is asked for again by hand.
      this.want = was === "p1" ? "auto" : was === "p2" ? "any" : "";
      this.deps.onRole("", null);
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

  private receive(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      this.deps.onSound(data);
      return;
    }
    if (typeof data !== "string") {
      return;
    }
    let msg: { role?: Role; game?: GameInfo | null };
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }
    if (msg.role !== undefined) {
      this._role = msg.role;
      if (msg.role === "p1" || msg.role === "p2") {
        this.want = msg.role === "p1" ? "auto" : "any";
      }
      this.deps.onRole(msg.role, msg.game ?? null);
    }
  }

  private send(): void {
    if (!this.connected) {
      return;
    }
    const buf = new ArrayBuffer(3);
    const view = new DataView(buf);
    this.seq = (this.seq + 1) & 0xff;
    view.setUint8(0, this.seq);
    view.setUint16(1, this.mask & 0xffff);
    this.sock!.send(buf);
  }

  private sendText(obj: unknown): void {
    if (this.connected) {
      this.sock!.send(JSON.stringify(obj));
    }
  }
}

/** ws:// or wss://, matching how the page itself was loaded. */
export function socketUrl(loc: { protocol: string; host: string }): string {
  return (loc.protocol === "https:" ? "wss://" : "ws://") + loc.host + "/api/game/socket";
}
