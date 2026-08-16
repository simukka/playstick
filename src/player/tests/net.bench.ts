import { bench, describe } from "vitest";
import { ApiClient } from "../src/net";

// The client itself is I/O-bound, so this only guards the request-shaping cost
// -- JSON.stringify of the body, assembling the {ok, data} result -- on the
// control path, against a synchronous fake transport.
const fetchFn = () =>
  Promise.resolve({ ok: true, json: () => Promise.resolve({ state: "playing" }) });

describe("ApiClient shaping", () => {
  bench("post() request assembly", async () => {
    const api = new ApiClient(fetchFn);
    for (let k = 0; k < 20000; k++) {
      await api.volume(k % 2 ? 10 : -10);
    }
  });
});
