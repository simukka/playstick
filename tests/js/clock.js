// The timecode model in playstick-ui.html: how the headphone audio is placed
// against the film, and what the loop does once it is.
//
// The daemon publishes where the film is, the instant on ITS clock when it was
// there, whether it is moving, and which timeline that belongs to. The page
// measures its own clock against the daemon's on a separate route (time.js)
// and evaluates the timecode against it. Everything below is about the second
// half of that: given a clock and a timeline, is the sound where the picture
// is, and does it stay there through the things that happen to a film.
//
// Written against a real capture. On 2026-08-02 an iPhone's telemetry showed
// the element planted 1.01 s behind the film at the top of playback and the
// rate command pinned at the +2% clamp for the whole 68 seconds, breaking up
// every few seconds as it fell off the clamp and back on. Section 9 replays
// that opening. It is the acceptance test for this model, not a historical
// note: the cause was a position the daemon had carried forward from a stale
// reading with no way to say so, and a timecode says so by construction.
//
// The script under test is the one that ships -- see tests/js/page.js.
const { install, load, check, done, run, acts, routes, state } =
  require("./page.js");

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

// The daemon's clock and the film on it. Both are lines this file defines, so
// every check is the page's belief against a truth known exactly.
const srv = { base: 918273.0, from: 0, session: "9f3c1a2b" };
const film = { start: 918270.0 };     // server clock at film position zero
let epoch = 1;

const srvNow = () => srv.base + (state.clock / 1000 - srv.from);
const filmAt = (t) => t - film.start;
const filmNow = () => filmAt(srvNow());

install("?debug");
// A round trip of zero, deliberately. What a real one costs the estimate is
// time.js's subject; here it would only mean the harness advancing the clock
// while the fake element does not play, which is an error this file would then
// be measuring instead of the controller's.
routes["/api/time"] = {
  rtt: 0,
  body: () => ({ now: srvNow(), session: srv.session }),
};
const P = load(PAGE, VARS, FNS);
run(2000);                            // the page's own opening burst

const tick = (ms) => { state.clock += ms === undefined ? 250 : ms; };

// The element as a plant, with the two properties that make it one.
//
// `rate` is the audio hardware's clock against this page's. They are not the
// same oscillator on iOS, which is the whole reason an integrator survives the
// rewrite: /api/time can measure this phone against the daemon and cannot see
// inside the phone at all. A DAC 60 ppm off is ordinary.
//
// `quantum` is that currentTime is reported on decoded-frame boundaries --
// 21.3 ms for AAC-LC -- so read at 4 Hz it is a staircase, not a line. ERR_LP
// exists for this, and a harness without it would let a loop that puts the
// staircase straight into playbackRate pass.
//
// `writeCost` is what a write to playbackRate costs on iOS. It lands on
// AVPlayer.rate through a WebContent->UIProcess round trip -- the syslog shows
// setPlaybackRate, PlaybackSessionModelContext::rateChanged and
// mediaPlayerRateChanged for every one -- which re-arms the render pipeline
// rather than gliding, and about 43 ms of audio never reaches the DAC. That is
// two AAC-LC frames, and it is measured rather than assumed: in the 2026-08-07
// capture `lag` was a median 43 ms on every telemetry line carrying a rate
// write and 1 ms on every line that did not.
//
// A harness that does not charge this lets the controller correct for free,
// and a loop whose own corrections are the fault it is correcting for looks
// perfectly stable here while it rails on the device.
const dac = { rate: 1, quantum: 0, writeCost: 0.043 };
let ctTrue = 0;
let lastRate = 1;

function quantise(t) {
  return dac.quantum ? Math.floor(t / dac.quantum) * dac.quantum : t;
}

// A real element plays while the clock runs, at whatever rate was commanded.
// Forgetting that makes every gap look like an error the page made.
function tickPlaying(ms) {
  const dt = ms === undefined ? 250 : ms;
  // A currentTime that is not where this left it is the page having seeked.
  if (Math.abs(P.snd.currentTime - quantise(ctTrue)) > 1e-9) {
    ctTrue = P.snd.currentTime;
  }
  // The plant pays for being re-commanded. Read the ELEMENT rather than
  // sndRateSet: that variable is what the page believes it asked for, and a
  // setRate() that updated the command without reaching snd.playbackRate has
  // to be visible here rather than invisible by construction.
  if (P.snd.playbackRate !== lastRate) {
    lastRate = P.snd.playbackRate;
    if (!P.snd.paused) { ctTrue -= dac.writeCost; }
  }
  state.clock += dt;
  if (!P.snd.paused) {
    ctTrue += (dt / 1000) * P.snd.playbackRate * dac.rate;
  }
  P.snd.currentTime = quantise(ctTrue);
}

// A status payload as the daemon builds it. `age` is how long ago the reading
// inside it was taken -- normally a fraction of a second, and the whole of
// section 9 is what happens when it is not.
function status(over) {
  const o = over || {};
  const at = srvNow() - (o.age || 0);
  const rate = o.rate === undefined ? 1 : o.rate;
  const out = {
    id: "abc", state: "playing", phone_audio: true, buffering: false,
    tracks: [{ n: 0, lang: "eng", offset: 0 }],
    position: filmAt(at), position_valid: true,
    timecode: { tc: o.tc === undefined ? filmAt(at) : o.tc, at: at,
                rate: rate, epoch: o.epoch === undefined ? epoch : o.epoch },
  };
  ["id", "state", "buffering", "phone_audio", "tracks", "position",
    "position_valid"].forEach(function (k) {
    if (k in o) { out[k] = o[k]; }
  });
  if (o.timecode !== undefined) { out.timecode = o.timecode; }
  return out;
}

function reset() {
  acts.length = 0;
  dac.rate = 1;
  dac.quantum = 0;
  // Off by default, and deliberately. Every section above this was written and
  // its thresholds set against a plant that changes speed for free, and each
  // of them is testing something other than what a write costs -- placement,
  // pausing, resuming, the crystal, the staircase. Turning the cost on for all
  // of them would move numbers that were argued out one at a time, which is a
  // separate decision from adding the coverage. Section 11 turns it on.
  dac.writeCost = 0;
  ctTrue = 0;
  lastRate = 1;
  epoch += 1;
  film.start = srvNow() - 10;         // ten seconds into the film
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
  P.snd.playbackRate = 1;
  P.snd.src = "/api/audio/abc/0";
  P.snd.currentTime = 0;
  P.snd.paused = true;
  P.sndSeekPending = true;
  P.tcBase = null;
}

// The offset, freshly locked, as a phone that has been on the page has.
function lockClock() {
  P.srvWin = [];
  P.srvBest = null;
  P.srvNextAt = 0;
  P.timeBurst(P.TIME_BURST);
  run(P.TIME_BURST * P.TIME_SPACING * 1000 + 100);
}

const errMs = () => (P.snd.currentTime - filmNow()) * 1000;

// --- 0. the two constants, against the arithmetic that sets them ----------

check("SEEK_LIMIT is inside one nudge-recovery a listener would sit through",
  P.SEEK_LIMIT / P.RATE_LIMIT <= 15,
  `${P.SEEK_LIMIT}s / ${P.RATE_LIMIT} = ${(P.SEEK_LIMIT / P.RATE_LIMIT).toFixed(0)}s of clamped slew`);
check("SEEK_LIMIT is above the worst standing error measured (216 ms)",
  P.SEEK_LIMIT > 0.216, `${P.SEEK_LIMIT}`);

// --- 1. one poll is a whole model ----------------------------------------

// The headline change. This used to take three polls to place the element and
// eight to trust the estimate, and a film opened with up to two seconds of
// silence because placing off a thin window planted a listener a second late.
// A timecode carries its own instant, so there is nothing to average.
reset();
lockClock();
P.audioStatus(status());
check("the first status starts the element", !P.snd.paused, acts.join(","));
const seeksBefore = P.sndSeeks;
tick();
P.sndCorrect();
check("...and the first tick places it, once",
  P.sndSeeks === seeksBefore + 1, "seeks=" + (P.sndSeeks - seeksBefore));
check("...on the film, to the millisecond", Math.abs(errMs()) < 1,
  errMs().toFixed(2) + " ms");

// --- 2. ...but not off a clock nobody has measured ------------------------

reset();
P.srvWin = [];
P.srvBest = null;
P.audioStatus(status());
check("a phone with no clock offset does not start", P.snd.paused,
  acts.join(",") || "(nothing)");
tick();
P.sndCorrect();
check("...and nothing is placed off it", P.sndSeeks === seeksBefore + 1);

// setDest(), setTrack() and tapToListen have to call play() inside the gesture
// or iOS withholds the permission for good, so the element is allowed to start
// and is parked a tick later instead.
P.snd.paused = false;
acts.length = 0;
tick();
P.sndCorrect();
check("a gesture start is parked until there is a clock to place it against",
  P.snd.paused, acts.join(",") || "(nothing)");

// --- 3. a film that is not moving ----------------------------------------

// The daemon states it now. This used to be inferred by watching whether the
// polled position had advanced by enough of the wall clock, which could not
// tell a demuxer that blinked from one that stopped.
reset();
lockClock();
P.audioStatus(status());
tickPlaying();
P.sndCorrect();
const parkedAt = P.snd.currentTime;
epoch += 1;
P.audioStatus(status({ state: "paused", rate: 0, tc: parkedAt }));
tickPlaying();
P.sndCorrect();
check("a paused film stops the sound with it", P.snd.paused,
  acts.join(",") || "(nothing)");
// Bounded by one correction tick, not by zero: the daemon says the timeline
// stopped and the loop hears it at its own cadence. What matters is that it
// is a quarter of a second and not a poll -- this used to be noticed in
// audioStatus(), four times slower -- and that it stops there.
const overrun = P.snd.currentTime - parkedAt;
check("...within one tick of audio, not one poll", overrun <= 0.25 + 1e-9,
  (overrun * 1000).toFixed(0) + " ms of overrun");
tickPlaying(3000);
P.sndCorrect();
check("...and then stays exactly where the daemon said, however long it sits",
  Math.abs(P.snd.currentTime - parkedAt - overrun) < 1e-9,
  ((P.snd.currentTime - parkedAt - overrun) * 1000).toFixed(0) + " ms of creep");

// A demuxer waiting on the NAS is the same fact with a different name.
reset();
lockClock();
P.audioStatus(status());
tickPlaying();
P.sndCorrect();
epoch += 1;
P.audioStatus(status({ buffering: true, rate: 0, tc: P.snd.currentTime }));
tickPlaying();
P.sndCorrect();
check("...and so does one whose demuxer is waiting on the NAS", P.snd.paused);

// --- 4. what a discontinuity costs ---------------------------------------

// The reason the epoch exists. A pause used to empty an eight-sample offset
// window that took eight polls to refill, and the audio was not allowed to
// make a sound for any of them. Now the clock is measured somewhere that does
// not care what the film is doing, and the resume costs one seek.
reset();
lockClock();
P.audioStatus(status());
tickPlaying();
P.sndCorrect();
const keptOff = P.srvBest;
const keptRatio = P.srvRatio;
epoch += 1;
P.audioStatus(status({ state: "paused", rate: 0, tc: P.snd.currentTime }));
tickPlaying();
P.sndCorrect();
epoch += 1;
film.start = srvNow() - 40;           // resumed, further into the film
const seeksAtResume = P.sndSeeks;
P.audioStatus(status());
check("a resume starts the sound again on the same poll", !P.snd.paused,
  acts.join(","));
tickPlaying();
P.sndCorrect();
check("...for the cost of one seek", P.sndSeeks === seeksAtResume + 1,
  (P.sndSeeks - seeksAtResume) + " seeks");
check("...landing on the film", Math.abs(errMs()) < 1,
  errMs().toFixed(2) + " ms");
check("...and the clock model was never in question",
  P.srvBest === keptOff && P.srvRatio === keptRatio);

// A different film is a different timeline, and still not a reason to
// re-measure two crystals.
reset();
lockClock();
P.audioStatus(status());
tickPlaying();
P.sndCorrect();
const acrossFilms = P.srvBest;
epoch += 1;
film.start = srvNow() - 3;
P.audioStatus(status({ id: "zzz" }));
tickPlaying();
P.sndCorrect();
check("a different film does not cost the clock offset",
  P.srvBest === acrossFilms);

// --- 5. SEEK_LIMIT --------------------------------------------------------

// An error the nudge cannot fix in time gets a cut, one it can does not. This
// is the escape hatch from a saturated clamp.
function errorOf(seconds) {
  reset();
  lockClock();
  P.audioStatus(status());
  tickPlaying();
  P.sndCorrect();
  tickPlaying();
  P.sndCorrect();
  const before = P.sndSeeks;
  P.snd.currentTime -= seconds;
  tickPlaying(0);
  P.sndCorrect();
  return { seeked: P.sndSeeks > before, err: P.sndErr };
}
const small = errorOf(0.2);
check("a 200 ms error is nudged, not cut", !small.seeked,
  "err=" + (small.err * 1000).toFixed(0) + "ms");
const big = errorOf(0.4);
check("a 400 ms error is cut", big.seeked,
  "err=" + (big.err * 1000).toFixed(0) + "ms");

// --- 6. a phone in a pocket ----------------------------------------------

// iOS keeps the element playing when the screen locks and suspends this page's
// timers, so the offset ages out with nobody home to refresh it. That element
// is running on the measured ratio, which is the one estimate a locked screen
// cannot take away. Silencing it every time somebody checked the time would be
// its own bug.
reset();
lockClock();
P.audioStatus(status());
tickPlaying();
P.sndCorrect();
P.sndSeekPending = false;
const beforePocket = P.sndSeeks;
state.clock += (P.OFF_MAX_AGE + 60) * 1000;
P.snd.currentTime += P.OFF_MAX_AGE + 60;
P.srvPick();
check("an offset that aged out is not an offset", P.srvNow() === null);
acts.length = 0;
for (let i = 0; i < 4; i++) { tickPlaying(); P.sndCorrect(); }
check("a pocketed phone keeps playing on the ratio", !P.snd.paused,
  acts.join(",") || "(nothing)");
check("...and is not placed off a clock that is not there",
  P.sndSeeks === beforePocket, "seeks=" + (P.sndSeeks - beforePocket));

// --- 7. steady state ------------------------------------------------------

// Every write to playbackRate costs ~43 ms of audio on iOS -- two AAC-LC
// frames, which is what a re-armed AVPlayer discards -- so the number of them
// is the thing to hold down.
// Two minutes of film, reported once a second, corrected four times a second.
// The first thirty are the placement transient; what matters is afterwards,
// which is where the capture spent all of its dropouts.
function settle() {
  lockClock();
  P.audioStatus(status());
  let writes = 0;
  let worst = 0;
  let sum = 0;
  let n = 0;
  for (let i = 0; i < 480; i++) {
    if (i % 4 === 0) { P.audioStatus(status()); }
    P.timeTick();
    tickPlaying();
    P.sndCorrect();
    if (i === 120) { writes = P.sndRateWrites; }
    if (i > 120) {
      if (Math.abs(P.sndErr) > worst) { worst = Math.abs(P.sndErr); }
      sum += P.sndRateSet - 1;
      n += 1;
    }
  }
  // The MEAN commanded rate, not the last one. RATE_EPS is a deadband on the
  // change, so a command that needs to sit between two writable values dithers
  // across them rather than settling on one -- and what the element actually
  // plays at is the average of that, not whichever side it stopped on.
  return { writes: P.sndRateWrites - writes, worst: worst, rate: sum / n };
}

// A phone whose DAC runs 60 ppm slow against its own CPU. That is not anything
// /api/time can see -- it is inside one device -- so finding it is exactly the
// job left to the integrator after the measured ratio has done the rest.
reset();
dac.rate = 1 - 60e-6;
const clean = settle();
check("the command finds the crystal the page cannot measure",
  Math.abs(clean.rate - 60e-6) < 25e-6,
  "mean rate=" + (clean.rate * 1e6).toFixed(0) + "ppm against a true 60");
check("the error stays inside the perception window",
  clean.worst < 0.045, "worst=" + (clean.worst * 1000).toFixed(0) + "ms");
check("and it is not writing playbackRate more than a few times a minute",
  clean.writes <= 6, clean.writes + " writes in the settled 90 seconds");

// The same, with currentTime reported on decoded-frame boundaries. The loop
// has to hold the sound in place while reading a staircase rather than a line.
reset();
dac.rate = 1 - 60e-6;
dac.quantum = 0.0213;
const stairs = settle();
check("a staircase clock does not move the sound out of the window",
  stairs.worst < 0.045, "worst=" + (stairs.worst * 1000).toFixed(0) + "ms");
check("...nor push the command anywhere near the clamp",
  Math.abs(stairs.rate) < P.RATE_LIMIT / 4,
  "mean rate=" + (stairs.rate * 1e6).toFixed(0) + "ppm");
// NOT asserted tightly, and deliberately: at a full 21.3 ms quantum this loop
// writes on about half its ticks, because ERR_LP leaves the command moving by
// ~90 ppm a tick against a RATE_EPS of 100. The 2026-08-02 capture shows a
// real iPhone writing once or twice a SECOND, so the device's own currentTime
// is smoother than a full-amplitude staircase -- but by how much is not
// something this file can know. What is bounded here is the failure that
// would matter: writing on every tick, which is 43 ms of audio each.
check("...and does not re-arm the pipeline on every single tick",
  stairs.writes < 360 * 0.75,
  stairs.writes + " writes in 360 ticks -- see the note above");

// --- 8. a phone whose crystal is wrong ------------------------------------

// The measured ratio is fed forward, so the integrator starts from a good
// guess rather than searching for one over a minute. A 100 ppm phone is the
// worst two consumer parts do, and it is 0.7 s over a feature film.
reset();
P.srvWin = [];
P.srvBest = null;
P.srvRatio = 150e-6;                  // as time.js measures it off /api/time
lockClock();
P.audioStatus(status());
tickPlaying();
P.sndCorrect();                       // places the element, and returns
tickPlaying();
P.sndCorrect();                       // ...and this is the first nudge
check("a measured crystal ratio reaches the element on the first nudge",
  Math.abs((P.sndRateSet - 1) - 150e-6) < 40e-6,
  "rate=" + ((P.sndRateSet - 1) * 1e6).toFixed(0) + "ppm against a measured 150");

// --- 9. THE CAPTURE, REPRODUCED ------------------------------------------

// At the top of a film the daemon's reading is old: mpv has only just started
// and the cached position is what there is. On 2026-08-02 the first sample was
// 1.19 s behind the truth, it went straight into the target, and the element
// was planted a second late -- an error that then had to come out through a
// nudge capped at 2%, against an applied offset limited to 18 ms a second. The
// capture spent all 68 seconds pinned at the clamp.
//
// The same reading, in a timecode: `at` says when it was taken, so a phone
// extrapolates the second the daemon could not.
reset();
lockClock();
film.start = srvNow() - 1.25;         // the film is 1.25 s in
P.audioStatus(status({ age: 1.19 })); // ...and the reading is 1.19 s old
tickPlaying();
P.sndCorrect();
check("a reading the daemon took a second ago does not plant the element "
  + "a second late", Math.abs(errMs()) < 10,
  "off by " + errMs().toFixed(0) + " ms, where the capture was 1180");

for (let i = 0; i < 120; i++) {
  if (i % 4 === 0) { P.audioStatus(status({ age: 0.4 })); }
  P.timeTick();
  tickPlaying();
  P.sndCorrect();
}
check("...and the loop does not end up pinned at the rate clamp",
  Math.abs(P.sndRateSet - 1) < P.RATE_LIMIT * 0.9,
  "rate=" + ((P.sndRateSet - 1) * 1e6).toFixed(0) + "ppm");
check("...with the sound where the picture is", Math.abs(P.sndErr) < 0.045,
  "err=" + (P.sndErr * 1000).toFixed(0) + "ms");

// --- 11. the 2026-08-07 capture: a loop correcting for its own corrections -

// The journal, in one paragraph. A startup error wound sndDrift to the -2%
// rail in twenty seconds and it stayed there for the whole film -- because the
// ERR_LP residual of the frame staircase kept pushing the command across
// RATE_EPS, and every one of those nudges threw away 43 ms of audio. That was
// 0.44 writes a second, 18725 ppm of lost time, against the 20000 ppm rail the
// integrator was holding to make up for it. The loop was the fault.
//
// The condition that makes it self-sustaining is worth writing down, because
// it is what says a smaller clamp is a workaround rather than a fix:
//
//     writes/s * dac.writeCost >= RATE_LIMIT
//
// Lowering RATE_LIMIT to 2000 ppm on the device did break it -- writes fell
// 0.435 -> 0.028/s and stalls 0.448 -> 0.035/s -- but only by a factor of 1.7,
// and it cost the P term the authority to walk out a real error: a 300 ms
// desync then took 65 s to close, and SEEK_LIMIT/RATE_LIMIT went to 250 s
// against the 15 s section 0 asserts. One constant cannot bound both the
// integrator, which models two crystals inside one phone and belongs in the
// tens of ppm, and the command, which has to be able to move.
//
// Hence DRIFT_LIMIT. What is checked here is the loop surviving its own
// corrections over a film's worth of ticks, with the clamp left where the
// recovery arithmetic in section 0 wants it.
function soak(ticks) {
  let drifted = 0;
  const before = P.sndRateWrites;
  let worst = 0;
  for (let i = 0; i < ticks; i++) {
    if (i % 4 === 0) { P.audioStatus(status()); }
    P.timeTick();
    tickPlaying();
    P.sndCorrect();
    if (i > 240) {                    // past the placement transient
      if (Math.abs(P.sndErr) > worst) { worst = Math.abs(P.sndErr); }
      if (Math.abs(P.sndDrift) > Math.abs(drifted)) { drifted = P.sndDrift; }
    }
  }
  return { writes: P.sndRateWrites - before, worst: worst, drift: drifted,
           secs: ticks * P.TICK / 1000 };
}

// Ten minutes, on a phone whose DAC is 60 ppm slow and whose currentTime comes
// back on frame boundaries -- i.e. an ordinary one, with nothing else wrong.
reset();
lockClock();
dac.writeCost = 0.043;                // the whole point of this section
dac.rate = 1 - 60e-6;
dac.quantum = 0.0213;
P.audioStatus(status());
const soaked = soak(2400);

check("the integrator does not wind up to the clamp when its own writes cost "
  + "audio", Math.abs(soaked.drift) < P.RATE_LIMIT * 0.5,
  "drift=" + (soaked.drift * 1e6).toFixed(0) + "ppm against a clamp of "
    + (P.RATE_LIMIT * 1e6).toFixed(0));
check("...so the command is not pinned at the clamp either",
  Math.abs(P.sndRateSet - 1) < P.RATE_LIMIT * 0.9,
  "rate=" + ((P.sndRateSet - 1) * 1e6).toFixed(0) + "ppm");
// The device wrote 0.435 times a second and lost 1.9% of the film to it. This
// is an eighth of that -- one artefact every twenty seconds or so, where the
// capture had one every two.
check("...and it is not writing playbackRate often enough to starve itself",
  soaked.writes / soaked.secs < 0.08,
  (soaked.writes / soaked.secs).toFixed(3) + " writes/s over "
    + soaked.secs.toFixed(0) + "s, against 0.435 on the device");
// Against 125 ms rather than 45. The two are not interchangeable and the
// difference is the point: a write throws audio away, so it can only ever put
// the sound BEHIND the picture, and the eye tolerates a lag of 125 ms where it
// catches a lead at 45. A single write is 43 ms by itself, so a bound of 45 ms
// here would not be a quality target -- it would be an assertion that the loop
// never actuates at all.
check("...with the sound inside the lag the eye tolerates",
  soaked.worst < 0.125, "worst=" + (soaked.worst * 1000).toFixed(0) + "ms");

// The other half of the capture: once the integrator is bounded, the command
// still has to have the authority to walk out a real desync. This is the
// regression that lowering RATE_LIMIT on the device introduced -- a 300 ms
// error under SEEK_LIMIT, which no seek will cut and a small clamp cannot
// close. Half of SEEK_LIMIT has to come out in the time section 0 allows.
reset();
lockClock();
dac.rate = 1;
dac.quantum = 0.0213;
P.audioStatus(status());
soak(400);                            // settle first
const desync = P.SEEK_LIMIT * 0.5;
ctTrue -= desync;                     // the film jumps; the element does not
P.snd.currentTime = quantise(ctTrue);
const budget = Math.ceil(P.SEEK_LIMIT / P.RATE_LIMIT) * 1000 / P.TICK;
let closed = false;
for (let i = 0; i < budget && !closed; i++) {
  if (i % 4 === 0) { P.audioStatus(status()); }
  P.timeTick();
  tickPlaying();
  P.sndCorrect();
  if (Math.abs(P.sndErr) < 0.045) { closed = true; }
}
check("a desync just under the seek threshold still closes inside the "
  + "recovery section 0 promises", closed,
  "err=" + (P.sndErr * 1000).toFixed(0) + "ms after "
    + (budget * P.TICK / 1000).toFixed(0) + "s of slew");

done();
