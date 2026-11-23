// The projector's settings view: that it draws what the daemon lists, sends one
// change at a time, redraws from the answer rather than from the tap, and says
// why when there is nothing to show. The daemon's side is tested in
// tests/test_projector_protocol.py and tests/test_projectionist.py.
import { describe, it, expect } from "vitest";
import { ASKING, OFF_TEXT, ProjectorSheet } from "../src/projector-sheet";
import { makePage } from "./harness/page";
import { asDocument, type FakeEl } from "./harness/dom";
import type { PostResult } from "../src/net";
import type { ProjectorSetting } from "../src/types";

const PICTURE: ProjectorSetting = {
  name: "picture",
  label: "Picture mode",
  value: "CN1",
  choices: [
    { value: "NOR", label: "Normal" },
    { value: "CN1", label: "Cinema 1" },
  ],
};
const BLANK: ProjectorSetting = {
  name: "blank",
  label: "Blank the picture",
  value: "0",
  choices: [
    { value: "0", label: "Off" },
    { value: "1", label: "On" },
  ],
};

const ok = (settings: ProjectorSetting[], power = "on"): PostResult => ({
  ok: true,
  data: { power, settings },
});

function sheet(first: PostResult) {
  const doc = makePage();
  const calls: string[] = [];
  const pending: ((r: PostResult) => void)[] = [];
  const s = new ProjectorSheet(asDocument(doc), {
    load: () => {
      calls.push("load");
      return Promise.resolve(first);
    },
    change: (name, value) => {
      calls.push(name + "=" + value);
      return new Promise((resolve) => pending.push(resolve));
    },
    back: () => calls.push("back"),
  });
  const list = () => doc.getElementById("projList") as FakeEl;
  const note = () => (doc.getElementById("projNote") as FakeEl).textContent;
  const heads = () =>
    list()
      .children.filter((c) => c.tagName.toLowerCase() === "h2")
      .map((c) => c.textContent);
  const group = (i: number) =>
    list().children.filter((c) => c.classList.contains("projGroup"))[i]!.children;
  const ticked = (i: number) =>
    group(i)
      .filter((r) => r.classList.contains("sel"))
      .map((r) => r.children[0]!.textContent);
  const back = () => doc.getElementById("projBack")!.children[0]! as FakeEl;
  return { s, calls, pending, note, heads, group, ticked, back };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("ProjectorSheet", () => {
  it("asks the projector on opening, and draws a group per setting", async () => {
    const v = sheet(ok([PICTURE, BLANK]));
    v.s.opened();
    expect(v.note()).toBe(ASKING);
    await tick();
    expect(v.calls).toEqual(["load"]);
    expect(v.heads()).toEqual(["Picture mode", "Blank the picture"]);
    expect(v.ticked(0)).toEqual(["Cinema 1"]);
    expect(v.ticked(1)).toEqual(["Off"]);
    expect(v.note()).toBe("");
  });

  it("says to switch on when the projector is in standby", async () => {
    const v = sheet(ok([], "standby"));
    v.s.opened();
    await tick();
    expect(v.heads()).toEqual([]);
    expect(v.note()).toBe(OFF_TEXT);
  });

  it("shows the daemon's sentence when it cannot ask", async () => {
    const v = sheet({ ok: false, data: { error: "I couldn't reach the projector." } });
    v.s.opened();
    await tick();
    expect(v.note()).toBe("I couldn't reach the projector.");
  });

  it("redraws from what the projector says after a change", async () => {
    const v = sheet(ok([PICTURE]));
    v.s.opened();
    await tick();
    v.group(0)[0]!.click();
    expect(v.calls).toEqual(["load", "picture=NOR"]);
    expect(v.ticked(0)).toEqual(["Cinema 1"]);
    v.pending[0]!(ok([{ ...PICTURE, value: "NOR" }]));
    await tick();
    expect(v.ticked(0)).toEqual(["Normal"]);
  });

  it("sends one change at a time", async () => {
    const v = sheet(ok([PICTURE, BLANK]));
    v.s.opened();
    await tick();
    v.group(0)[0]!.click();
    v.group(1)[1]!.click();
    expect(v.calls).toEqual(["load", "picture=NOR"]);
  });

  it("does not resend the value already chosen", async () => {
    const v = sheet(ok([PICTURE]));
    v.s.opened();
    await tick();
    v.group(0)[1]!.click();
    expect(v.calls).toEqual(["load"]);
  });

  it("keeps the old tick and says why when a change is refused", async () => {
    const v = sheet(ok([PICTURE]));
    v.s.opened();
    await tick();
    v.group(0)[0]!.click();
    v.pending[0]!({ ok: false, data: { error: "The projector didn't take that." } });
    await tick();
    expect(v.ticked(0)).toEqual(["Cinema 1"]);
    expect(v.note()).toBe("The projector didn't take that.");
    // ...and the next tap goes through.
    v.group(0)[0]!.click();
    expect(v.calls).toEqual(["load", "picture=NOR", "picture=NOR"]);
  });

  it("goes back to the logo's menu", () => {
    const v = sheet(ok([]));
    v.back().click();
    expect(v.calls).toEqual(["back"]);
  });
});
