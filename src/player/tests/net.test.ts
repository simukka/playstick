import { describe, it, expect } from "vitest";
import { ApiClient } from "../src/net";

interface Call {
  url: string;
  init?: { method?: string; headers?: Record<string, string>; body?: string };
}

function client(reply: (call: Call) => { ok: boolean; body: unknown }) {
  const calls: Call[] = [];
  const fetchFn = (url: string, init?: unknown) => {
    const call: Call = { url, init: init as Call["init"] };
    calls.push(call);
    const { ok, body } = reply(call);
    return Promise.resolve({ ok, json: () => Promise.resolve(body) });
  };
  return { calls, api: new ApiClient(fetchFn) };
}

describe("ApiClient reads", () => {
  it("fetches status without a header by default", async () => {
    const { calls, api } = client(() => ({ ok: true, body: { state: "idle" } }));
    const s = await api.status();
    expect(s.state).toBe("idle");
    expect(calls[0]!.url).toBe("/api/status");
    expect(calls[0]!.init).toBeUndefined();
  });

  it("carries the sync telemetry header when given one", async () => {
    const { calls, api } = client(() => ({ ok: true, body: { state: "idle" } }));
    await api.status("v=2;id=abc");
    expect(calls[0]!.init!.headers!["X-Playstick-Sync"]).toBe("v=2;id=abc");
  });

  it("reads the library and the clock", async () => {
    const { api } = client((c) =>
      c.url === "/api/library"
        ? { ok: true, body: { items: [{ id: "a", title: "A" }], available: true } }
        : { ok: true, body: { now: 123.5, session: "s1" } },
    );
    expect((await api.library()).items).toHaveLength(1);
    expect((await api.time()).session).toBe("s1");
  });
});

describe("ApiClient control posts", () => {
  it("sends JSON bodies and reports ok plus the parsed reason", async () => {
    const { calls, api } = client((c) =>
      c.url === "/api/play"
        ? { ok: false, body: { error: "A film is already playing." } }
        : { ok: true, body: { state: "playing" } },
    );

    const refused = await api.play("abc");
    expect(refused.ok).toBe(false);
    expect(refused.data.error).toBe("A film is already playing.");
    expect(calls[0]!.init!.method).toBe("POST");
    expect(JSON.parse(calls[0]!.init!.body!)).toEqual({ id: "abc" });

    const paused = await api.pause();
    expect(paused.ok).toBe(true);
    expect(JSON.parse(calls[1]!.init!.body!)).toEqual({});

    await api.volume(-10);
    expect(JSON.parse(calls[2]!.init!.body!)).toEqual({ delta: -10 });
  });
});
