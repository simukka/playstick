// The game socket from the phone's side: what goes up, when, and what a
// reconnect asks for.
import { describe, it, expect } from "vitest";
import { GameLink, HEARTBEAT_MS, RETRY_MS, socketUrl, type SocketLike } from "../src/game-link";

class FakeSocket implements SocketLike {
  binaryType = "";
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  sent: (string | ArrayBuffer)[] = [];
  closed = false;
  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
  }
  // The test's side.
  up(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  down(): void {
    this.readyState = 3;
    this.onclose?.();
  }
  say(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  get texts(): unknown[] {
    return this.sent.filter((d) => typeof d === "string").map((d) => JSON.parse(d as string));
  }
  get masks(): number[] {
    return this.sent
      .filter((d): d is ArrayBuffer => d instanceof ArrayBuffer)
      .map((d) => new DataView(d).getUint16(1));
  }
}

function link() {
  const sockets: FakeSocket[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const beats: (() => void)[] = [];
  const roles: string[] = [];
  const sounds: ArrayBuffer[] = [];
  const l = new GameLink({
    url: "ws://stick/api/game/socket",
    open: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    schedule: (fn, ms) => timers.push({ fn, ms }),
    cancel: () => {},
    every: (fn, ms) => {
      expect(ms).toBe(HEARTBEAT_MS);
      beats.push(fn);
      return 1;
    },
    stopEvery: () => beats.splice(0),
    onRole: (role) => roles.push(role),
    onSound: (pcm) => sounds.push(pcm),
  });
  return { l, sockets, timers, beats, roles, sounds };
}

describe("GameLink", () => {
  it("asks for player one when it opens, and sends the mask", () => {
    const { l, sockets } = link();
    l.open();
    sockets[0]!.up();
    expect(sockets[0]!.texts).toEqual([{ join: "auto" }]);
    l.setMask(5);
    expect(sockets[0]!.masks.at(-1)).toBe(5);
  });

  it("sends nothing for an unchanged mask, but repeats it on the heartbeat", () => {
    const { l, sockets, beats } = link();
    l.open();
    sockets[0]!.up();
    l.setMask(3);
    const n = sockets[0]!.masks.length;
    l.setMask(3);
    expect(sockets[0]!.masks.length).toBe(n);
    beats[0]!();
    expect(sockets[0]!.masks.length).toBe(n + 1);
  });

  it("hands roles and sound to its owner", () => {
    const { l, sockets, roles, sounds } = link();
    l.open();
    sockets[0]!.up();
    sockets[0]!.say({ role: "p2", game: { players: 2 } });
    sockets[0]!.onmessage?.({ data: new ArrayBuffer(4) });
    expect(roles).toEqual(["p2"]);
    expect(l.role).toBe("p2");
    expect(sounds).toHaveLength(1);
  });

  it("reconnects, and reclaims player one only if it had it", () => {
    const { l, sockets, timers } = link();
    l.open();
    sockets[0]!.up();
    sockets[0]!.say({ role: "listener" });
    sockets[0]!.down();
    expect(timers[0]!.ms).toBe(RETRY_MS);
    timers[0]!.fn();
    sockets[1]!.up();
    expect(sockets[1]!.texts).toEqual([]);

    sockets[1]!.say({ role: "p1" });
    sockets[1]!.down();
    timers[1]!.fn();
    sockets[2]!.up();
    expect(sockets[2]!.texts).toEqual([{ join: "auto" }]);
  });

  it("stays closed once closed", () => {
    const { l, sockets, timers } = link();
    l.open();
    sockets[0]!.up();
    l.close();
    expect(sockets[0]!.closed).toBe(true);
    expect(timers).toHaveLength(0);
  });

  it("speaks wss on an https page", () => {
    expect(socketUrl({ protocol: "https:", host: "stick.example" })).toBe(
      "wss://stick.example/api/game/socket",
    );
    expect(socketUrl({ protocol: "http:", host: "10.0.0.5" })).toBe(
      "ws://10.0.0.5/api/game/socket",
    );
  });
});
