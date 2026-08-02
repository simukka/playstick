// The ?debug telemetry in playstick-ui.html: the header a listening phone
// sends on every status poll, and the stall detector behind two of its fields.
//
// Most of these are negatives. A stall detector that counts a seek, a
// deliberate slowdown, or iOS's frame quantisation as a dropout does not
// report a fault -- it manufactures one, in the log that the next fix will be
// argued from.
const { install, load, check, done, state } = require("./page.js");

install("?debug");
const P = load(process.argv[2] || __dirname + "/../../roles/player/files/playstick-ui.html",
  ["debugSync", "sndDest", "sndClockOff", "sndClockOffRaw", "sndOffsets",
    "sndErr", "sndDrift", "sndRateSet", "sndLastRtt", "sndPrevAt",
    "sndRateWrites", "telAheadMin", "telErrPeak", "telRateStep", "snd"],
  ["syncTelemetry", "sndWatchClock", "sndAhead", "sndResetClock"]);

const parse = (s) => Object.fromEntries(
  s.split(";").map((p) => { const i = p.indexOf("="); return [p.slice(0, i), p.slice(i + 1)]; }));

// What the daemon's filter will accept. Anything outside it is silently
// stripped there, so a field that needed it would arrive mangled.
const HEADER_SAFE = /^[A-Za-z0-9=;:.,+_-]*$/;

check("debug is on", P.debugSync === true);

// 1. Not listening at all.
let blob = P.syncTelemetry();
check("not listening -> st=off", parse(blob).st === "off", blob);
check("value is header-safe", HEADER_SAFE.test(blob));

// 2. Listening, no track loaded yet.
P.sndDest = "device";
blob = P.syncTelemetry();
check("no src -> st=idle", parse(blob).st === "idle", blob);

// 3. Playing, with a buffer and a known error.
P.snd.src = "/api/audio/0123456789abcdef/0";
P.snd.paused = false;
P.snd.currentTime = 1421.79;
P.snd.setBuffered([[1400.0, 1470.0]]);
P.sndClockOff = 0;
P.sndClockOffRaw = 0;
P.sndOffsets = [0, 0, 0];
P.sndErr = -0.038;
P.sndDrift = -0.00068;
P.sndRateSet = 1 - 0.000712;
P.sndLastRtt = 0.024;

// Four ticks of the correction loop, as one poll interval really contains.
for (let i = 0; i < 4; i++) {
  state.clock += 250;
  P.snd.currentTime += 0.25 * P.sndRateSet;
  P.sndWatchClock();
  const a = P.sndAhead();
  if (a !== null && (P.telAheadMin === null || a < P.telAheadMin)) { P.telAheadMin = a; }
}
P.telErrPeak = -0.041;
P.telRateStep = 0.00014;
P.sndRateWrites = 1;

blob = P.syncTelemetry();
const f = parse(blob);
console.log("\n  " + blob + "\n");
check("value is header-safe", HEADER_SAFE.test(blob), blob);
check("st=play", f.st === "play");
check("currentTime reported", Math.abs(parseFloat(f.ct) - P.snd.currentTime) < 0.01, f.ct);
check("buffer ahead reported", Math.abs(parseFloat(f.ahead) - (1470.0 - P.snd.currentTime)) < 0.15, f.ahead);
check("buffer low-water mark <= now", parseFloat(f.amin) <= parseFloat(f.ahead) + 0.05, f.amin);
check("error in ms", f.err === "-38", f.err);
check("signed peak error kept", f.errp === "-41", f.errp);
check("rate as ppm", f.rate === "-712", f.rate);
check("drift as ppm", f.drift === "-680", f.drift);
check("rate writes counted", f.w === "1", f.w);
check("largest write in ppm", f.dw === "140", f.dw);
check("readyState", f.rs === "4");
check("no stall on a clean run", f.ls === "0" && parseFloat(f.lag) < 30,
  "lag=" + f.lag + " ls=" + f.ls);
check("rtt in ms", f.rtt === "24", f.rtt);

// 4. Counters describe the interval, not the film. The correction loop runs
//    four times per poll, so a line that sampled would miss three quarters of
//    what happened.
blob = P.syncTelemetry();
const g = parse(blob);
check("counts reset after a send", g.w === "0" && g.sk === "0" && g.wt === "0", blob);
check("peaks reset after a send", g.errp === "0" && g.dw === "0", blob);

// 5. A real stall: the element's clock stops while wall time runs on.
state.clock += 250;
P.sndWatchClock();
const h = parse(P.syncTelemetry());
check("a stalled clock is detected", h.ls === "1", "ls=" + h.ls);
check("...and its size is reported", Math.abs(parseFloat(h.lag) - 250) < 5, "lag=" + h.lag);

// 6. A deliberate slowdown is not a stall.
P.sndRateSet = 0.98;
P.sndPrevAt = null;
for (let i = 0; i < 4; i++) {
  state.clock += 250;
  P.snd.currentTime += 0.25 * 0.98;
  P.sndWatchClock();
}
const j = parse(P.syncTelemetry());
check("a 2% slowdown is not counted as a stall", j.ls === "0", "ls=" + j.ls + " lag=" + j.lag);

// 7. Nor is frame quantisation: iOS reports currentTime on 21.3 ms boundaries,
//    so a 4 Hz reader sees a staircase.
P.sndRateSet = 1;
P.sndPrevAt = null;
let exact = P.snd.currentTime;
for (let i = 0; i < 20; i++) {
  state.clock += 250;
  exact += 0.25;
  P.snd.currentTime = Math.floor(exact / 0.0213) * 0.0213;
  P.sndWatchClock();
}
const k = parse(P.syncTelemetry());
check("frame quantisation is not counted as a stall", k.ls === "0", "ls=" + k.ls + " lag=" + k.lag);

// 8. Nor is a hard seek -- the loudest artefact this code makes on purpose.
P.sndPrevAt = null;
state.clock += 250; P.snd.currentTime += 0.25; P.sndWatchClock();
P.snd.currentTime -= 5;
P.sndResetClock();
state.clock += 250; P.snd.currentTime += 0.25; P.sndWatchClock();
const m = parse(P.syncTelemetry());
check("a seek is not counted as a stall", m.ls === "0", "ls=" + m.ls + " lag=" + m.lag);

// 9. Nothing non-finite ever reaches the header.
P.snd.currentTime = NaN;
P.sndLastRtt = Infinity;
const n = P.syncTelemetry();
check("non-finite values become empty fields",
  HEADER_SAFE.test(n) && !/NaN|Infinity/.test(n), n);

done();
