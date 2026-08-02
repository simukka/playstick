// The clock model in playstick-ui.html: how the headphone audio is placed
// against the film, and what the loop does once it is.
//
// Written against a real capture. On 2026-08-02 an iPhone's telemetry showed
// the element planted 1.01 s behind the film at the top of playback and the
// rate command pinned at the +2% clamp for the whole 68 seconds, breaking up
// every few seconds as it fell off the clamp and back on. Section 8 replays
// that opening; the rest holds the two guards that came out of it.
const { install, load, check, done, acts, state } = require("./page.js");

install("?debug");
const P = load(process.argv[2] || __dirname + "/../../roles/player/files/playstick-ui.html",
  ["snd", "sndDest", "sndOffsets", "sndClockOff", "sndClockOffRaw",
    "sndSeekPending", "sndErr", "sndDrift", "sndRateSet", "sndSeeks",
    "sndRateWrites", "sndTrackOffset", "sndTrim", "sndErrF", "sndPrevAt",
    "sndTracks", "sndFilmId", "sndTrackN", "sndNeedGesture", "sndLastPos",
    "sndLastPosAt", "SEEK_LIMIT", "SEEK_SAMPLES", "RATE_LIMIT"],
  ["sndCorrect", "sndClockReady", "sndSample", "sndResetClock", "audioStatus"]);

const tick = (ms) => { state.clock += ms === undefined ? 250 : ms; };
// A real element plays while the clock runs, at whatever rate was commanded.
// Forgetting that makes every gap look like an error the page made.
function tickPlaying(ms) {
  const dt = ms === undefined ? 250 : ms;
  state.clock += dt;
  if (!P.snd.paused) { P.snd.currentTime += (dt / 1000) * P.sndRateSet; }
}

function reset() {
  acts.length = 0;
  P.sndDest = "device";
  P.sndTracks = [{ n: 0, lang: "eng", offset: 0 }];
  P.sndFilmId = "abc";
  P.sndTrackN = 0;
  P.sndTrackOffset = 0;
  P.sndTrim = 0;
  P.sndNeedGesture = false;
  P.snd.src = "/api/audio/abc/0";
  P.snd.currentTime = 0;
  P.snd.paused = true;
  P.sndResetClock();
}

const status = (pos) => ({
  id: "abc", state: "playing", position: pos, position_valid: true,
  phone_audio: true, buffering: false, tracks: [{ n: 0, lang: "eng", offset: 0 }],
});

// 0. The two constants, against the arithmetic that sets them.
check("SEEK_LIMIT is inside one nudge-recovery a listener would sit through",
  P.SEEK_LIMIT / P.RATE_LIMIT <= 15,
  `${P.SEEK_LIMIT}s / ${P.RATE_LIMIT} = ${(P.SEEK_LIMIT / P.RATE_LIMIT).toFixed(0)}s of clamped slew`);
check("SEEK_LIMIT is above the worst standing error measured (216 ms)",
  P.SEEK_LIMIT > 0.216, `${P.SEEK_LIMIT}`);

// 1. One sample is not a clock model.
reset();
P.audioStatus(status(0.06), 0.02);
check("one sample does not start the element", P.snd.paused, acts.join(","));
check("...and the clock is not ready", P.sndClockReady() === false);
tick();
P.sndCorrect();
check("...and nothing was seeked", P.sndSeeks === 0);

// 2. Nor is two: the second sample is what first exposes a stale first one,
//    the third is what stops an unlucky pair from being the whole model.
tick(1000);
P.audioStatus(status(1.07), 0.02);
check("two samples still do not start it", P.snd.paused, "ns=" + P.sndOffsets.length);

// 3. The third releases it, and the first tick places it.
tick(1000);
P.audioStatus(status(2.07), 0.02);
check("the third sample starts the element", !P.snd.paused, acts.join(","));
const seeksBefore = P.sndSeeks;
tick();
P.sndCorrect();
check("the element is placed exactly once", P.sndSeeks === seeksBefore + 1);
check("...on the film, not on zero", Math.abs(P.snd.currentTime - 2.3) < 0.4,
  "ct=" + P.snd.currentTime.toFixed(2));

// 4. setDest(), setTrack() and tapToListen have to call play() inside the
//    gesture or iOS withholds the permission for good, so the element is
//    allowed to start and is parked a tick later instead.
reset();
P.snd.paused = false;
acts.length = 0;
tick();
P.sndCorrect();
check("a gesture start is parked while the model is thin", P.snd.paused,
  acts.join(",") || "(nothing)");
check("...without placing it first", P.sndSeeks === seeksBefore + 1);

// 5. A phone coming out of a pocket must NOT be silenced. visibilitychange
//    empties the offset window there on purpose while keeping the clock ratio,
//    and the element is already running on that ratio.
reset();
for (let i = 0; i < 4; i++) { tick(1000); P.audioStatus(status(i), 0.02); }
tick(); P.sndCorrect();
P.sndDrift = 0.0004;
P.snd.paused = false;
P.sndOffsets = [];
P.sndClockOff = null; P.sndClockOffRaw = null; P.sndErrF = null;
P.sndSeekPending = false;
acts.length = 0;
for (let i = 0; i < 4; i++) { tick(); P.sndCorrect(); }
check("a pocket wake keeps playing on the clock ratio", !P.snd.paused,
  acts.join(",") || "(nothing)");
check("...and is not placed off the refilling window",
  P.sndSeeks === seeksBefore + 2, "seeks=" + P.sndSeeks);

// 6. SEEK_LIMIT: an error the nudge cannot fix in time gets a cut, one it can
//    does not. This is the escape hatch from a saturated clamp.
function errorOf(seconds) {
  reset();
  for (let i = 0; i < 4; i++) { tick(1000); P.audioStatus(status(10 + i), 0.02); }
  tickPlaying(); P.sndCorrect();
  tickPlaying(); P.sndCorrect();
  const before = P.sndSeeks;
  P.snd.currentTime -= seconds;
  tickPlaying(0); P.sndCorrect();
  return { seeked: P.sndSeeks > before, err: P.sndErr };
}
const small = errorOf(0.2);
check("a 200 ms error is nudged, not cut", !small.seeked,
  "err=" + (small.err * 1000).toFixed(0) + "ms");
const big = errorOf(0.4);
check("a 400 ms error is cut", big.seeked,
  "err=" + (big.err * 1000).toFixed(0) + "ms");

// 7. Steady state. Every write to playbackRate costs ~43 ms of audio on iOS,
//    so the number of them is the thing to hold down.
reset();
for (let i = 0; i < 4; i++) { tick(1000); P.audioStatus(status(i), 0.02); }
let film = 4;
let writesBefore = 0;
for (let i = 0; i < 480; i++) {
  if (i % 4 === 0) { film += 1; P.audioStatus(status(film), 0.02); }
  tickPlaying();
  P.sndCorrect();
  // The first thirty seconds are the placement transient. What matters is
  // afterwards, which is where the capture spent all of its dropouts.
  if (i === 120) { writesBefore = P.sndRateWrites; }
}
check("the rate command is nowhere near the clamp",
  Math.abs(P.sndRateSet - 1) < P.RATE_LIMIT / 2,
  "rate=" + ((P.sndRateSet - 1) * 1e6).toFixed(0) + "ppm");
check("the error stays inside the perception window",
  Math.abs(P.sndErr) < 0.045, "err=" + (P.sndErr * 1000).toFixed(0) + "ms");
check("and it is not writing playbackRate more than a few times a minute",
  P.sndRateWrites - writesBefore <= 6,
  (P.sndRateWrites - writesBefore) + " writes in the settled 90 seconds");

// 8. THE CAPTURE, REPRODUCED. At the top of a film the daemon's cached
//    position is stale -- it extrapolates from the last reading mpv gave it,
//    and mpv has only just started. On 2026-08-02 the first sample was 1.19 s
//    behind the truth. The max filter in sndSample() is what rejects a sample
//    like that: lateness can only ever make an offset look SMALLER, so a
//    fresher sample beats it. A max over one sample rejects nothing.
reset();
const truth = [1.25, 2.26, 3.30, 4.31];
const reported = [0.06, 2.26, 3.30, 4.31];
for (let i = 0; i < 4; i++) {
  tick(1000);
  P.audioStatus(status(reported[i]), 0.02);
  if (!P.snd.paused) { break; }
}
tickPlaying(); P.sndCorrect();
const filmNow = truth[P.sndOffsets.length - 1] +
  (state.clock / 1000 - Math.floor(state.clock / 1000));
check("a stale first position does not plant the element a second late",
  Math.abs(P.snd.currentTime - filmNow) < 0.25,
  "off by " + ((P.snd.currentTime - filmNow) * 1000).toFixed(0) + " ms");

for (let i = 0; i < 120; i++) {
  if (i % 4 === 0) { P.audioStatus(status(4.31 + i / 4), 0.02); }
  tickPlaying();
  P.sndCorrect();
}
check("...and the loop does not end up pinned at the rate clamp",
  Math.abs(P.sndRateSet - 1) < P.RATE_LIMIT * 0.9,
  "rate=" + ((P.sndRateSet - 1) * 1e6).toFixed(0) + "ppm");

done();
