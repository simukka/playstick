// The calibration socket from the phone's side: newest only, a scene sent once
// per change, and a reconnect that tells the projector where the phone is.
import { describe, it, expect } from "vitest";
import { CalibrateLink, RETRY_MS, TICK_MS, calibrateUrl, type Telemetry } from "../src/calibrate-link";
import type { SocketLike } from "../src/game-link";

class FakeSocket implements SocketLike {
  binaryType = "";
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  sent: string[] = [];
  closed = false;
  send(data: string | ArrayBuffer): void {
    this.sent.push(data as string);
  }
  close(): void {
    this.closed = true;
  }
  up(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  down(): void {
    this.readyState = 3;
    this.onclose?.();
  }
  get messages(): Record<string, unknown>[] {
    return this.sent.map((d) => JSON.parse(d));
  }
}

const tel = (a: number): Telemetry => ({ a, b: 0, g: 0, angle: 0, touch: null, mask: 0, dot: null });

function link() {
  const sockets: FakeSocket[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const l = new CalibrateLink({
    url: "ws://stick/api/calibrate/socket",
    open: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    schedule: (fn, ms) => timers.push({ fn, ms }),
    cancel: () => {},
    every: (_fn, ms) => {
      expect(ms).toBe(TICK_MS);
      return 1;
    },
    stopEvery: () => {},
  });
  return { l, sockets, timers };
}

describe("CalibrateLink", () => {
  it("sends only the newest reading each tick", () => {
    const { l, sockets } = link();
    l.open();
    sockets[0]!.up();
    for (let i = 0; i < 10; i++) l.setTel(tel(i));
    l.flush();
    expect(sockets[0]!.messages).toEqual([{ tel: tel(9) }]);
  });

  it("sends a scene once per change", () => {
    const { l, sockets } = link();
    l.open();
    sockets[0]!.up();
    l.setScene({ step: "sweep", edge: "left" });
    l.flush();
    l.setScene({ step: "sweep", edge: "left" });
    l.flush();
    l.setScene({ step: "sweep", edge: "right" });
    l.flush();
    expect(sockets[0]!.messages.map((m) => (m["scene"] as { edge: string }).edge)).toEqual(["left", "right"]);
  });

  it("tells a new socket the scene it missed", () => {
    const { l, sockets, timers } = link();
    l.open();
    sockets[0]!.up();
    l.setScene({ step: "tilt" });
    l.flush();
    sockets[0]!.down();
    expect(timers[0]!.ms).toBe(RETRY_MS);
    timers[0]!.fn();
    sockets[1]!.up();
    expect(sockets[1]!.messages).toEqual([{ scene: { step: "tilt" } }]);
  });

  it("says done, and stays closed once closed", () => {
    const { l, sockets, timers } = link();
    l.open();
    sockets[0]!.up();
    l.done();
    l.close();
    expect(sockets[0]!.messages).toEqual([{ done: true }]);
    expect(sockets[0]!.closed).toBe(true);
    expect(timers).toHaveLength(0);
  });

  it("speaks wss on an https page", () => {
    expect(calibrateUrl({ protocol: "https:", host: "stick" })).toBe("wss://stick/api/calibrate/socket");
  });
});
