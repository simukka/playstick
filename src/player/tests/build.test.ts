import { describe, it, expect } from "vitest";
import { ReloadPolicy } from "../src/build";

function policy() {
  const reloads: number[] = [];
  const p = new ReloadPolicy({ reload: () => reloads.push(1) }, "here");
  return { p, reloads };
}

describe("ReloadPolicy", () => {
  it("does nothing on a page the daemon never stamped", () => {
    const { p, reloads } = policy();
    expect(p.consider(undefined, "idle")).toBe(false);
    expect(reloads).toHaveLength(0);
  });

  it("does nothing while the build still matches", () => {
    const { p, reloads } = policy();
    expect(p.consider("here", "idle")).toBe(false);
    expect(reloads).toHaveLength(0);
  });

  it("reloads on a mismatch from a resting state", () => {
    const { p, reloads } = policy();
    expect(p.consider("there", "idle")).toBe(true);
    expect(reloads).toHaveLength(1);
  });

  it.each(["playing", "paused", "preparing"] as const)(
    "will not reload while %s -- there is something to lose",
    (state) => {
      const { p, reloads } = policy();
      expect(p.consider("there", state)).toBe(false);
      expect(reloads).toHaveLength(0);
    },
  );

  it("reloads once the film ends, having deferred during playback", () => {
    const { p, reloads } = policy();
    expect(p.consider("there", "playing")).toBe(false);
    expect(p.consider("there", "idle")).toBe(true);
    expect(reloads).toHaveLength(1);
  });

  it("asks for a navigation only once, though later polls still see the gap", () => {
    const { p, reloads } = policy();
    expect(p.consider("there", "idle")).toBe(true);
    expect(p.consider("there", "idle")).toBe(false);
    expect(p.consider("there", "idle")).toBe(false);
    expect(reloads).toHaveLength(1);
  });
});
