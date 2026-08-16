import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { makeShell } from "./harness/page";
import { installStyles } from "../src/styles";
import { asDocument } from "./harness/dom";

// The page is built by the views, not by the template, so what is worth checking
// is that building them produces the elements the rest of the code reaches for --
// and that the shell they are poured into still has its one marker.
const html = readFileSync(new URL("../template.html", import.meta.url), "utf8");

describe("the built page", () => {
  it("puts the grid view and the sheet chrome on the page", () => {
    const shell = makeShell();
    const doc = shell.doc;
    for (const id of ["library", "grid", "empty", "emptyClear", "filterChip",
                      "filterBtn", "audioBtn", "sheetScrim"]) {
      expect(doc.getElementById(id), id).not.toBeNull();
    }
  });

  it("installs one stylesheet carrying the tokens every view uses", () => {
    const doc = makeShell().doc;
    installStyles(asDocument(doc));
    expect(doc.head.children).toHaveLength(1);
    const css = doc.head.children[0]!.textContent;
    expect(css).toContain("--accent");
    expect(css).toContain("#grid");
    expect(css).toContain(".sheet");
    // Nothing drives the curator view any more; its rules went with it.
    expect(css).not.toContain(".editBtn");
  });
});

describe("template.html", () => {
  it("is a shell: the page's markup comes from the bundle", () => {
    expect(html).not.toContain("<style");
    expect(html).not.toContain('<div id=');
  });

  it("has exactly one bundle marker and no build token before the build", () => {
    expect((html.match(/\/\/__BUNDLE__/g) || []).length).toBe(1);
    expect(html).not.toContain("__PLAYSTICK_BUILD__");
  });
});
