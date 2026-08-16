import { describe, it, expect } from "vitest";
import { Grid, setText, toggleClass, h } from "../src/dom";
import { FakeDocument, FakeEl, asDocument, asEl } from "./harness/dom";
import type { TileItem } from "../src/dom";

function grid() {
  const doc = new FakeDocument();
  const parent = new FakeEl("div");
  const plays: string[] = [];
  const g = new Grid(
    asDocument(doc),
    asEl(parent),
    (p) => p + "?v=b",
    (id) => plays.push(id),
    () => 42,
  );
  return { doc, parent, plays, g };
}

const it3: TileItem[] = [
  { id: "a", title: "Akira" },
  { id: "b", title: "Brave" },
  { id: "c", title: "Cat" },
];

function order(parent: FakeEl): string[] {
  // The label text is the film title; use it to read the rendered order.
  return parent.children.map((c) => c.children[1]!.textContent);
}

describe("Grid reconciler", () => {
  it("creates one tile per film", () => {
    const { parent, g, doc } = grid();
    g.update(it3);
    expect(g.size).toBe(3);
    expect(order(parent)).toEqual(["Akira", "Brave", "Cat"]);
    // one button + img + span per tile
    expect(doc.creates).toBe(9);
  });

  it("touches the DOM zero times on an unchanged update", () => {
    const { parent, g, doc } = grid();
    g.update(it3);
    const inserts = parent.inserts;
    const removes = parent.removes;
    const creates = doc.creates;
    g.update(it3);
    g.update(it3);
    expect(parent.inserts).toBe(inserts);
    expect(parent.removes).toBe(removes);
    expect(doc.creates).toBe(creates); // no tiles rebuilt
  });

  it("reuses tiles and moves the minimum on a reorder", () => {
    const { parent, g, doc } = grid();
    g.update(it3);
    const creates = doc.creates;
    const inserts = parent.inserts;
    g.update([
      { id: "c", title: "Cat" },
      { id: "a", title: "Akira" },
      { id: "b", title: "Brave" },
    ]);
    expect(order(parent)).toEqual(["Cat", "Akira", "Brave"]);
    expect(doc.creates).toBe(creates); // nothing rebuilt
    expect(parent.inserts - inserts).toBe(1); // one node moved
  });

  it("adds and removes films without rebuilding the rest", () => {
    const { parent, g, doc } = grid();
    g.update(it3);
    const creates = doc.creates;
    g.update([
      { id: "a", title: "Akira" },
      { id: "c", title: "Cat" },
    ]);
    expect(order(parent)).toEqual(["Akira", "Cat"]);
    expect(parent.removes).toBe(1);
    g.update([...it3, { id: "d", title: "Dune" }]);
    expect(order(parent)).toEqual(["Akira", "Brave", "Cat", "Dune"]);
    // only the two new tiles (b re-added, d) cost creates: 2 tiles * 3 nodes
    expect(doc.creates - creates).toBe(6);
  });

  it("swaps a poster in when it finishes extracting", () => {
    const { parent, g } = grid();
    g.update([{ id: "a", title: "Akira", has_thumb: false }]);
    const img = parent.children[0]!.children[0]!;
    expect(img.src).toBe("/api/thumb/a?v=b");
    g.update([{ id: "a", title: "Akira", has_thumb: true }]);
    expect(img.src).toBe("/api/thumb/a?v=b&t=42");
  });

  it("plays the film a tile was tapped for", () => {
    const { parent, g, plays } = grid();
    g.update(it3);
    parent.children[1]!.click();
    expect(plays).toEqual(["b"]);
  });
});

describe("dom helpers", () => {
  it("setText writes only on a change", () => {
    const el = new FakeEl("span");
    setText(asEl(el), "hi");
    expect(el.textContent).toBe("hi");
    el.textContent = "hi"; // pretend a layout happened
    const before = el.childNodes.length;
    setText(asEl(el), "hi");
    expect(el.childNodes.length).toBe(before);
  });

  it("h builds an element with its attributes, children and handler", () => {
    const doc = new FakeDocument();
    let taps = 0;
    const built = h(
      asDocument(doc),
      "button",
      { id: "stop", class: "big", text: "STOP", type: "button", aria: "Stop", onTap: () => taps++ },
      [h(asDocument(doc), "span", { class: "tick", html: "\u2713" })],
    ) as unknown as FakeEl;
    expect(built.tagName).toBe("button");
    expect(built.id).toBe("stop");
    expect(built.classList.contains("big")).toBe(true);
    expect(built.ariaLabel).toBe("Stop");
    expect(built.children[0]!.innerHTML).toBe("\u2713");
    built.click();
    expect(taps).toBe(1);
  });

  it("h registers an id, so the page it builds resolves by id", () => {
    const doc = new FakeDocument();
    expect(doc.getElementById("grid")).toBeNull();
    h(asDocument(doc), "div", { id: "grid" });
    expect(doc.getElementById("grid")).not.toBeNull();
  });

  it("toggleClass reflects state", () => {
    const el = new FakeEl("div");
    toggleClass(asEl(el), "on", true);
    expect(el.classList.contains("on")).toBe(true);
    toggleClass(asEl(el), "on", false);
    expect(el.classList.contains("on")).toBe(false);
  });
});
