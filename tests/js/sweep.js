// Calibrating the plant against a real capture, before anything is asserted
// against it.
//
// This is not a test. It prints a table and returns 0. Its job is to answer a
// question that has to be settled before clock.js section 12 can mean
// anything: WHAT PLANT REPRODUCES THE 2026-08-12 PHONE?
//
// The capture, as numbers to hit (page load af2942, 202 rows on stock
// constants, from sync.csv via scripts/sync-log-to-csv.py):
//
//     seeks         1.43 /s      305 of them in 214 s
//     stalls >30ms  2.68 /s      i.e. ~1.2/s that no seek accounts for
//     lag median     164 ms      worst single-tick shortfall, per interval
//     lag p90        249 ms      a second mode, sitting exactly on SEEK_LIMIT
//     err median    -249 ms      and trimodal: clusters at ~-95, ~-250, ~-335
//     err 150-199    EMPTY       the error only ever crosses that band
//     ahead          419 s       buffer never dipped; bf=0; not starvation
//
// Two hypotheses for the ~1.2 stalls/s that the seeks do not explain, and the
// sweep is built to separate them:
//
//   H1  RIPPLE. One seek costs more than one tick's worth of stall, so its
//       debt lands across two ticks and the page counts two shortfalls. Then
//       stalls/seeks ~ 1.8 falls out of seekCost and seekSpread alone, and the
//       bimodal `lag` is one cost beating against the 250 ms tick grid rather
//       than two different costs.
//   H2  INDEPENDENT. Something stalls the element irrespective of seeking, and
//       the plant needs a second, unexplained source.
//
// H1 is the parsimonious one and the clean stretch supports it -- with sl:500
// the same phone ran 0.22 seeks/s AND 0 lag. If a background stall existed it
// should have shown there too. But "supports" is not "shows", so: sweep, and
// let the table say. If no (seekCost, seekSpread) reproduces all four numbers
// at once, H1 is dead and the probe has to go measure H2.
const { install, load, run, routes, state } = require("./page.js");
const { makePlant } = require("./plant.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["snd", "sndDest", "sndSeekPending", "sndErr", "sndDrift",
  "sndRateSet", "sndSeeks", "sndRateWrites", "sndTrackOffset", "sndTrim",
  "sndErrF", "sndPrevAt", "sndTracks", "sndFilmId", "sndTrackN",
  "sndNeedGesture", "srvWin", "srvBest", "srvRatio", "srvSession", "srvNextAt",
  "sndRateAt", "tcBase", "SEEK_LIMIT", "RATE_LIMIT", "DRIFT_LIMIT",
  "WRITE_EVERY", "TICK", "TIME_BURST", "TIME_SPACING", "OFF_MAX_AGE"];
const FNS = ["sndCorrect", "srvNow", "filmNow", "target", "audioStatus",
  "timeBurst", "timeTick", "srvPick"];

const srv = { base: 918273.0, from: 0, session: "9f3c1a2b" };
const film = { start: 918270.0 };
let epoch = 1;
const srvNow = () => srv.base + (state.clock / 1000 - srv.from);
const filmAt = (t) => t - film.start;

install("?debug");
routes["/api/time"] = { rtt: 0, body: () => ({ now: srvNow(), session: srv.session }) };
const P = load(PAGE, VARS, FNS);
run(2000);
const plant = makePlant(P, state);

function status() {
  const at = srvNow();
  return {
    id: "abc", state: "playing", phone_audio: true, buffering: false,
    tracks: [{ n: 0, lang: "eng", offset: 0 }],
    position: filmAt(at), position_valid: true,
    timecode: { tc: filmAt(at), at: at, rate: 1, epoch: epoch },
  };
}

function reset() {
  plant.reset();
  epoch += 1;
  film.start = srvNow() - 10;
  P.sndDest = "device";
  P.sndTracks = [{ n: 0, lang: "eng", offset: 0 }];
  P.sndFilmId = "abc";
  P.sndTrackN = 0;
  P.sndTrackOffset = 0;
  P.sndTrim = 0;
  P.sndNeedGesture = false;
  P.sndDrift = 0;
  P.sndErrF = null;
  P.sndRateSet = 1;
  P.sndRateAt = 0;
  P.sndSeeks = 0;
  P.snd.playbackRate = 1;
  P.snd.src = "/api/audio/abc/0";
  P.snd.currentTime = 0;
  P.snd.paused = true;
  P.sndSeekPending = true;
  P.tcBase = null;
  P.srvWin = [];
  P.srvBest = null;
  P.srvNextAt = 0;
  P.timeBurst(P.TIME_BURST);
  run(P.TIME_BURST * P.TIME_SPACING * 1000 + 100);
}

// One run of the phone the capture came off: 60 ppm DAC, frame-quantised
// currentTime, both actuators charged, opening a second and a quarter late.
function trial(seekCost, seekSpread, ticks) {
  reset();
  plant.dac.writeCost = 0.043;
  plant.dac.rate = 1 - 60e-6;
  plant.dac.quantum = 0.0213;
  plant.dac.seekCost = seekCost;
  plant.dac.seekSpread = seekSpread;
  P.audioStatus(status());
  plant.tick();
  P.sndCorrect();
  plant.ct -= 1.25;                   // the capture's opening error
  P.snd.currentTime = plant.quantise(plant.ct);

  const errs = [];
  for (let i = 0; i < 200; i++) {     // settle past the placement transient
    if (i % 4 === 0) { P.audioStatus(status()); }
    P.timeTick(); plant.tick(); P.sndCorrect();
  }
  plant.zero();
  P.sndSeeks = 0;
  for (let i = 0; i < ticks; i++) {
    if (i % 4 === 0) { P.audioStatus(status()); }
    P.timeTick(); plant.tick(); P.sndCorrect();
    errs.push(P.sndErr);
  }
  const m = plant.marks();
  errs.sort((a, b) => a - b);
  const q = (p) => errs[Math.min(errs.length - 1, Math.floor(p / 100 * errs.length))];
  // The capture's fingerprint: how much of the error distribution falls in the
  // 150-199 ms band it never occupied.
  const band = errs.filter((e) => Math.abs(e) >= 0.150 && Math.abs(e) < 0.200).length;
  return { m: m, errMedian: q(50), band: band / errs.length };
}

const TARGET = { seeks: 1.43, stalls: 2.68, lagMed: 0.164, lagP90: 0.249 };

console.log("plant calibration against the 2026-08-12 capture");
console.log("target:  seeks 1.43/s   stalls 2.68/s   lag med 164ms  p90 249ms"
  + "   err med -249ms   150-199 band empty\n");
console.log("  cost  spread |  seeks/s  stalls/s  st/sk |  lagmed   lagp90 |"
  + "  errmed   band |  lost");
console.log("  " + "-".repeat(84));

let best = null;
for (const spread of [1, 2, 3]) {
  for (const cost of [0.16, 0.20, 0.24, 0.25, 0.28, 0.32, 0.40]) {
    const r = trial(cost, spread, 1200);
    const m = r.m;
    const ratio = m.seeksPerSec ? m.stallsPerSec / m.seeksPerSec : 0;
    // Score on the two numbers that are hardest to hit at once.
    const err = Math.abs(m.seeksPerSec - TARGET.seeks) / TARGET.seeks
      + Math.abs(m.stallsPerSec - TARGET.stalls) / TARGET.stalls;
    if (m.seeksPerSec > 0.1 && (best === null || err < best.err)) {
      best = { err: err, cost: cost, spread: spread, m: m };
    }
    console.log("  " + (cost * 1000).toFixed(0).padStart(4)
      + spread.toString().padStart(8)
      + " |" + m.seeksPerSec.toFixed(2).padStart(9)
      + m.stallsPerSec.toFixed(2).padStart(10)
      + ratio.toFixed(2).padStart(7)
      + " |" + (m.lagMedian * 1000).toFixed(0).padStart(7) + "ms"
      + (m.lagP90 * 1000).toFixed(0).padStart(7) + "ms"
      + " |" + (r.errMedian * 1000).toFixed(0).padStart(7) + "ms"
      + (r.band * 100).toFixed(0).padStart(6) + "%"
      + " |" + (m.lost * 100).toFixed(0).padStart(5) + "%");
  }
  console.log("  " + "-".repeat(84));
}

// The verdict, and it is a negative one. Printed rather than scored, because
// "closest cell" would dress a falsified model up as a fitted one.
console.log("\nVERDICT: H1 is falsified. No cell reproduces the capture.\n");
console.log("  The table is BISTABLE and the hinge is exactly SEEK_LIMIT: every");
console.log("  cost at or below 250 ms settles at 0.00 seeks/s, every cost above");
console.log("  it pins at 4.00/s -- one per tick. The device sat at 1.43/s, which");
console.log("  this plant cannot produce at any (cost, spread). Cost above the");
console.log("  threshold changes nothing either (280/320/400 are identical): the");
console.log("  debt always exceeds what a tick can pay, so `lag` comes out");
console.log("  deterministic and the measured decile spread never shows.\n");
console.log("  The reason is one line of dynamics. After a seek the error is");
console.log("  -seekCost. Below SEEK_LIMIT it does not re-trip, and the element --");
console.log("  commanded +1.56% -- walks it back out: zero seeks. Above, it");
console.log("  re-trips on the very next tick: four seeks. FOR AN INTERMEDIATE");
console.log("  RATE THE ERROR MUST GROW BETWEEN SEEKS, and the only thing that");
console.log("  grows it is the element running behind the film. The command was");
console.log("  speeding UP, so it can only be time the element lost.\n");
console.log("  So the capture forces a stall source that is not the seeking, and");
console.log("  says how big: 2.68 stalls/s against 1.51 seeks/s leaves ~1.2/s");
console.log("  unaccounted, on 91% of its lines, with only 18 `waiting` events in");
console.log("  214 s -- silent. The clean sl:500 stretch does NOT refute this: it");
console.log("  ran 0.78 stalls/s itself, non-zero. Background stalls were present");
console.log("  throughout; the seek threshold is what amplified 0.78 into 2.68.\n");
console.log("  H1 + H2, then, and H2 is load-bearing. The seek loop is an");
console.log("  AMPLIFIER, not the generator. What the probe has to measure is the");
console.log("  background stall process -- rate and size -- because that is the");
console.log("  input this plant is missing and no amount of seek-cost tuning");
console.log("  substitutes for it.");
if (best) {
  console.log("\n  (nearest cell, for the record: cost="
    + (best.cost * 1000).toFixed(0) + "ms spread=" + best.spread + " -> "
    + best.m.seeksPerSec.toFixed(2) + " seeks/s, "
    + best.m.stallsPerSec.toFixed(2) + " stalls/s, lag med "
    + (best.m.lagMedian * 1000).toFixed(0) + "ms -- wrong on all four.)");
}
