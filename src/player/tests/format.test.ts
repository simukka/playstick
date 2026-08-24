import { describe, it, expect } from "vitest";
import { timeLeft, barWidth } from "../src/format";

describe("timeLeft", () => {
  it("formats hours and minutes", () => {
    expect(timeLeft(3600 + 24 * 60)).toBe("1 h 24 min left");
    expect(timeLeft(2 * 3600)).toBe("2 h left");
    expect(timeLeft(45 * 60)).toBe("45 min left");
    expect(timeLeft(30)).toBe("1 min left"); // rounds up off zero
  });

  it("never goes negative", () => {
    expect(timeLeft(-500)).toBe("0 min left");
  });
});

describe("barWidth", () => {
  it("is a clamped percentage", () => {
    expect(barWidth(0, 100)).toBe("0.0%");
    expect(barWidth(50, 100)).toBe("50.0%");
    expect(barWidth(100, 100)).toBe("100.0%");
    expect(barWidth(200, 100)).toBe("100.0%");
  });

  it("is 0 before a duration is known", () => {
    expect(barWidth(10, 0)).toBe("0");
  });
});
