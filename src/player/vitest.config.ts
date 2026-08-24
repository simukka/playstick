import { defineConfig } from "vitest/config";

// node environment on purpose: the controllers under test take their clock,
// storage, network and audio element as injected seams, so there is nothing to
// gain from jsdom and a great deal of startup cost to lose on it. The DOM-facing
// modules are exercised through a tiny hand-rolled stub, same philosophy as the
// old tests/js harness.
//
// The exception is tests/page.test.ts, which boots the built page in a real DOM
// because that is the whole point of it. It constructs its own JSDOM rather than
// switching the environment, so the other twenty files keep their fast start.
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    benchmark: {
      include: ["tests/**/*.bench.ts"],
    },
  },
});
