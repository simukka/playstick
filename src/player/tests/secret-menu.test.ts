// The menu hidden in the logo: that a tap does not open it and a hold does, and
// that what it offers follows the projector and the film. The routes behind it
// are tested on the daemon's side (tests/test_pointer.py).
import { describe, it, expect } from "vitest";
import { HOLD_MS, SecretSheet, holdToOpen } from "../src/secret-menu";
import { makePage } from "./harness/page";
import { asDocument, asEl, type FakeEl } from "./harness/dom";
import type { PostResult } from "../src/net";
import type { Status } from "../src/types";

function timers() {
  const queue: { fn: () => void; ms: number; id: number }[] = [];
  let next = 1;
  return {
    queue,
    schedule: (fn: () => void, ms: number) => {
      const id = next++;
      queue.push({ fn, ms, id });
      return id;
    },
    cancel: (id: number) => {
      const i = queue.findIndex((t) => t.id === id);
      if (i >= 0) {
        queue.splice(i, 1);
      }
    },
    fire: () => queue.splice(0).forEach((t) => t.fn()),
  };
}

function logo() {
  const doc = makePage();
  const el = asDocument(doc).createElement("h1") as unknown as FakeEl;
  const t = timers();
  let opened = 0;
  holdToOpen(asEl(el), () => opened++, t);
  return { el, t, opened: () => opened };
}

describe("holdToOpen", () => {
  it("opens after a held touch", () => {
    const l = logo();
    l.el.fire("pointerdown", { pointerType: "touch", clientX: 10, clientY: 10 });
    expect(l.t.queue[0]!.ms).toBe(HOLD_MS);
    l.t.fire();
    expect(l.opened()).toBe(1);
  });

  it("does nothing for a tap -- including the click a tap sends", () => {
    const l = logo();
    l.el.fire("pointerdown", { pointerType: "touch" });
    l.el.fire("pointerup");
    l.el.fire("click");
    l.t.fire();
    expect(l.opened()).toBe(0);
  });

  it("does nothing for a finger that slides off to scroll", () => {
    const l = logo();
    l.el.fire("pointerdown", { pointerType: "touch", clientX: 10, clientY: 10 });
    l.el.fire("pointermove", { clientX: 10, clientY: 40 });
    l.t.fire();
    expect(l.opened()).toBe(0);
  });

  it("forgives a finger that trembles", () => {
    const l = logo();
    l.el.fire("pointerdown", { pointerType: "touch", clientX: 10, clientY: 10 });
    l.el.fire("pointermove", { clientX: 13, clientY: 14 });
    l.t.fire();
    expect(l.opened()).toBe(1);
  });

  it("opens on a mouse click at once", () => {
    const l = logo();
    l.el.fire("pointerdown", { pointerType: "mouse" });
    l.el.fire("click");
    expect(l.opened()).toBe(1);
    expect(l.t.queue).toHaveLength(0);
  });
});

function menu(status: Partial<Status>) {
  const doc = makePage();
  const calls: string[] = [];
  const answer = (data: PostResult["data"], ok = true): Promise<PostResult> =>
    Promise.resolve({ ok, data });
  const sheet = new SecretSheet(asDocument(doc), {
    power: (on) => {
      calls.push(on ? "on" : "off");
      return answer({ message: on ? "Switching on." : "Switching off." });
    },
    wakeClock: () => {
      calls.push("clock");
      return answer({ message: "The clock is back." });
    },
    openPointer: () => calls.push("pointer"),
  });
  sheet.onStatus({
    state: "idle",
    projector: { model: "pt-ae4000", power: "standby", fault: "" },
    ...status,
  } as Status);
  sheet.opened();
  const rows = () => (doc.getElementById("secretList") as FakeEl).children;
  const labels = () => rows().map((r) => r.children[0]!.textContent);
  const subs = () => rows().map((r) => r.children[0]!.children[0]?.textContent ?? "");
  const note = () => (doc.getElementById("secretNote") as FakeEl).textContent;
  return { sheet, calls, rows, labels, subs, note };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("SecretSheet", () => {
  it("offers the three things", () => {
    expect(menu({}).labels()).toEqual(["Turn projector on", "Show clock", "Pointer"]);
  });

  it("offers off when the lamp is lit", () => {
    const m = menu({ projector: { model: "pt-ae4000", power: "on", fault: "" } });
    expect(m.labels()[0]).toBe("Turn projector off");
  });

  it("leaves the projector out when there is none", () => {
    const m = menu({ projector: { model: "none", power: "unknown", fault: "" } });
    expect(m.labels()).toEqual(["Show clock", "Pointer"]);
  });

  it("will not switch off under a film, and says why", () => {
    const m = menu({
      state: "playing",
      projector: { model: "pt-ae4000", power: "on", fault: "" },
    });
    expect(m.subs()[0]).toBe("Stop the film first");
    m.rows()[0]!.click();
    expect(m.calls).toEqual([]);
    expect(m.note()).toBe("Stop the film first");
  });

  it("will not show the clock under a film", () => {
    const m = menu({ state: "playing" });
    m.rows()[1]!.click();
    expect(m.calls).toEqual([]);
  });

  it("shows the projector's fault", () => {
    const m = menu({
      projector: { model: "pt-ae4000", power: "unknown", fault: "I couldn't reach the projector." },
    });
    expect(m.subs()[0]).toBe("I couldn't reach the projector.");
  });

  it("does what a row says, and shows the daemon's answer", async () => {
    const m = menu({});
    m.rows()[0]!.click();
    await tick();
    expect(m.calls).toEqual(["on"]);
    expect(m.note()).toBe("Switching on.");
    m.rows()[1]!.click();
    await tick();
    expect(m.note()).toBe("The clock is back.");
    m.rows()[2]!.click();
    expect(m.calls).toEqual(["on", "clock", "pointer"]);
  });
});
