import { describe, it, expect } from "vitest";
import { Telemetry, type TelemetrySnapshot } from "../src/telemetry";

function snap(over: Partial<TelemetrySnapshot>): TelemetrySnapshot {
  return {
    now: 12.3,
    listening: true,
    hasSrc: true,
    paused: false,
    hidden: false,
    currentTime: 100.25,
    errMs: 5,
    ratePpm: 150,
    ratioPpm: 60,
    driftPpm: 10,
    offsetMs: 12.5,
    rttMs: 3,
    samples: 40,
    epoch: 7,
    writes: 0,
    seeks: 0,
    waits: 0,
    buffering: 0,
    digest: "",
    ...over,
  };
}

describe("Telemetry", () => {
  it("says off when not listening and idle when nothing is loaded", () => {
    const t = new Telemetry(() => "abc123");
    expect(t.build(snap({ listening: false }))).toBe("v=2;id=abc123;t=12.3;st=off");
    expect(t.build(snap({ hasSrc: false }))).toBe("v=2;id=abc123;t=12.3;st=idle");
  });

  it("emits one k=v line with the playback metrics", () => {
    const t = new Telemetry(() => "abc123");
    const line = t.build(snap({}));
    expect(line).toContain("st=play");
    expect(line).toContain("err=5");
    expect(line).toContain("rate=150");
    expect(line).toContain("ratio=60");
    expect(line).toContain("ep=7");
    expect(line).toContain("tun=");
  });

  it("reports counts as deltas since the previous line", () => {
    const t = new Telemetry(() => "x");
    t.build(snap({ writes: 10, seeks: 2, waits: 1 }));
    const line = t.build(snap({ writes: 13, seeks: 2, waits: 4 }));
    expect(line).toContain(";w=3");
    expect(line).toContain(";sk=0");
    expect(line).toContain(";wt=3");
  });

  it("reports the worst error since the last line, then clears it", () => {
    const t = new Telemetry(() => "x");
    t.recordErrMs(-40);
    t.recordErrMs(12);
    const line = t.build(snap({ errMs: 3 }));
    expect(line).toContain("errp=-40"); // peak kept its sign and magnitude
    t.recordErrMs(3); // a fresh interval starts empty and is fed by the ticks
    const next = t.build(snap({ errMs: 3 }));
    expect(next).toContain("errp=3");
  });

  it("blanks a field that is not a finite number", () => {
    const t = new Telemetry(() => "x");
    const line = t.build(snap({ offsetMs: null, rttMs: null }));
    expect(line).toContain(";off=;");
    expect(line).toContain(";ort=;");
  });

  it("carries the tunables digest", () => {
    const t = new Telemetry(() => "x");
    expect(t.build(snap({ digest: "kp:200" }))).toContain("tun=kp:200");
  });
});
