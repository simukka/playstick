// Measuring this phone's clock against the daemon's.
//
// The layer everything else in the audio path now stands on. It answers one
// question -- what time is it on the machine playing the film -- and it
// answers it out of round trips to /api/time, which means the thing being
// tested is a measurement made over a network whose delay is the error.
//
// So the driver owns the network. `net.rtt` is the round trip, changed between
// samples; the daemon's clock is a line the driver defines and can run fast or
// slow on purpose. Every check below is the page's estimate against a truth
// this file knows exactly.
//
// The script under test is the one that ships -- see tests/js/page.js.
const { install, load, check, done, run, routes, state } = require("./page.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["srvWin", "srvBest", "srvRatio", "srvSession", "srvNextAt",
  "tcBase", "snd", "sndDest", "sndSeekPending", "sndTracks", "sndFilmId",
  "TIME_BURST", "TIME_EVERY", "TIME_SPACING", "OFF_MAX_AGE", "RATIO_SPAN",
  "RATIO_MIN"];
const FNS = ["srvNow", "filmNow", "timeTick", "timeSample", "timeBurst",
  "srvPick"];

// The daemon's clock, as this file defines it. `rate` is how fast it runs
// against the phone's: 1 + 40e-6 is a daemon whose crystal gains 40 ppm on
// this phone's, which is an ordinary pair of consumer parts.
const srv = { base: 918273.0, from: 0, rate: 1, session: "9f3c1a2b" };
// `rtt` is the symmetric part of the round trip. `skew` is delay on ONE leg,
// which is the part that actually poisons an estimate: the page assumes the
// answer was true at the midpoint, so a one-sided hold of s reads the offset
// high by s/2 and no amount of arithmetic on that one sample can find it.
// `jitter` picks a fresh skew per request from a repeatable sequence, because
// a network with no jitter would let a filter that does nothing pass.
const net = { rtt: 6, skew: 0, jitter: 0, seed: 12345, hits: [] };

function truth() {
  return srv.base + (state.clock / 1000 - srv.from) * srv.rate;
}

function nextSkew() {
  if (!net.jitter) { return net.skew; }
  // Deterministic, so a failure is the same failure tomorrow.
  net.seed = (net.seed * 1103515245 + 12345) & 0x7fffffff;
  return net.skew + (net.seed % 1000) / 1000 * net.jitter;
}

install("");
routes["/api/time"] = {
  get rtt() { return net.rtt; },
  body: function () {
    // The harness has already advanced half the symmetric round trip. Moving
    // the clock here puts the rest of the delay on the reply leg, which is
    // what makes this an asymmetric path rather than a tidy one.
    state.clock += nextSkew();
    net.hits.push(state.clock);
    return { now: truth(), session: srv.session };
  },
};
const P = load(PAGE, VARS, FNS);
// The page fires its own burst on load, exactly as it does in a browser. Let
// that finish before anything below counts requests, so the first check is
// measuring a burst rather than the tail of one.
run(2000);

function reset(over) {
  srv.base = (over && over.base) || 918273.0;
  srv.from = state.clock / 1000;
  srv.rate = (over && over.rate) || 1;
  srv.session = (over && over.session) || "9f3c1a2b";
  net.rtt = (over && over.rtt) || 6;
  net.skew = (over && over.skew) || 0;
  net.jitter = (over && over.jitter) || 0;
  net.seed = 12345;
  net.hits.length = 0;
  P.srvWin = [];
  P.srvBest = null;
  P.srvRatio = 0;
  P.srvSession = "";
  P.srvNextAt = 0;
  P.tcBase = null;
}

// Samples at the cadence the page would take them at, without needing the
// element or a film: timeTick() is what the correction loop calls.
function sample(seconds) {
  const end = state.clock + seconds * 1000;
  while (state.clock < end) {
    P.timeTick();
    run(250);                         // one TICK
  }
}

const offMs = () => (P.srvNow() - state.clock / 1000) * 1000;
const trueOffMs = () => (truth() - state.clock / 1000) * 1000;
const ppm = (r) => (r * 1e6).toFixed(1);

// --- nothing is known until something has been measured -------------------

reset();
check("a phone that has not asked yet does not claim to know the time",
  P.srvNow() === null, String(P.srvNow()));
check("...and there is no film clock without one", P.filmNow() === null);

// --- the burst ------------------------------------------------------------

// The offset is what gates the first sound, so how long it takes to lock is a
// second of silence at the top of a film or it is not.
reset();
P.timeBurst(P.TIME_BURST);
run(P.TIME_BURST * P.TIME_SPACING * 1000 + 100);
check("the opening burst locks the clock in under a second",
  P.srvBest !== null && net.hits.length === P.TIME_BURST,
  net.hits.length + " samples in " +
  ((net.hits[net.hits.length - 1] - net.hits[0]) / 1000).toFixed(2) + "s");
check("...to within a millisecond of the truth",
  Math.abs(offMs() - trueOffMs()) < 1,
  "off by " + (offMs() - trueOffMs()).toFixed(2) + " ms");

// Chained, not fired together. Parallel requests queue behind each other and
// inflate the round trip that is the error bar on everything here.
let overlapped = false;
for (let i = 1; i < net.hits.length; i++) {
  if (net.hits[i] - net.hits[i - 1] < net.rtt) { overlapped = true; }
}
check("...one request at a time, never a parallel burst", !overlapped,
  "closest pair " + Math.min.apply(null, net.hits.slice(1).map(
    (h, i) => h - net.hits[i])).toFixed(0) + " ms apart");

// --- the quickest exchange wins -------------------------------------------

// Delay is one-sided: a packet can be held up and never hurried. So the
// fastest round trip is the one with the least room to be wrong, and averaging
// it against slower ones can only move it away from the truth.
// A reply held up on one leg by 200 ms reads the offset 100 ms high, which is
// most of the perception window on its own.
reset({ rtt: 4, skew: 200 });
P.timeSample();
run(1);
const slowErr = Math.abs(offMs() - trueOffMs());
check("a one-sided delay really does poison a single sample", slowErr > 90,
  slowErr.toFixed(0) + " ms out");

net.skew = 0;
P.srvNextAt = 0;
P.timeSample();
run(1);
check("a quick exchange wins on arrival", P.srvBest.rtt * 1000 < 5,
  "rtt=" + (P.srvBest.rtt * 1000).toFixed(1) + "ms");
check("...and takes the estimate back to the truth",
  Math.abs(offMs() - trueOffMs()) < 1,
  "now " + (offMs() - trueOffMs()).toFixed(2) + " ms out, from " +
  slowErr.toFixed(0));

const locked = P.srvBest;
net.skew = 400;
P.srvNextAt = 0;
P.timeSample();
run(1);
check("...and a slow one afterwards does not disturb it",
  P.srvBest === locked, "rtt=" + (P.srvBest.rtt * 1000).toFixed(0) + "ms");
check("...so a congested network cannot walk the offset off the truth",
  Math.abs(offMs() - trueOffMs()) < 1,
  (offMs() - trueOffMs()).toFixed(2) + " ms out");

// --- the crystal ratio ----------------------------------------------------

// The slope of the offset against time IS the ratio between the two clocks.
// Nothing is inferred from the audio error to get it.
reset({ rate: 1 + 40e-6, rtt: 4 });
sample(P.RATIO_SPAN);
check("the clock ratio is measured, not searched for",
  Math.abs(P.srvRatio - 40e-6) < 8e-6,
  ppm(P.srvRatio) + " ppm against a true 40.0");

reset({ rate: 1 - 90e-6, rtt: 4 });
sample(P.RATIO_SPAN);
check("...in either direction", Math.abs(P.srvRatio + 90e-6) < 12e-6,
  ppm(P.srvRatio) + " ppm against a true -90.0");

// The measurement that has to survive a real house: a wire whose delay wanders
// by 60 ms sample to sample, which is 30 ms of bias on each estimate. Over a
// three-minute baseline that is the difference between a usable ratio and a
// number. This is what the fit's rejection of slow samples is for.
reset({ rate: 1 + 40e-6, rtt: 4, jitter: 60 });
sample(P.RATIO_SPAN);
check("...and through a network that jitters by tens of milliseconds",
  Math.abs(P.srvRatio - 40e-6) < 25e-6,
  ppm(P.srvRatio) + " ppm against a true 40.0");

// A slope needs a baseline. Below one there is no honest answer, and a number
// invented from thirty seconds of jitter would be worse than none.
reset({ rate: 1 + 40e-6, rtt: 4 });
sample(P.RATIO_MIN / 2);
check("a span too short to fit reports no ratio at all", P.srvRatio === 0,
  ppm(P.srvRatio) + " ppm off " + P.srvWin.length + " samples");

// --- staleness ------------------------------------------------------------

// An offset is a statement about now, and the winner carries an error that
// grows with its age. Past OFF_MAX_AGE the page stops claiming to know.
reset({ rtt: 4 });
sample(P.RATIO_SPAN);
const ratioKept = P.srvRatio;
state.clock += (P.OFF_MAX_AGE + 5) * 1000;
P.srvPick();
check("an offset nobody has refreshed goes quiet rather than stale",
  P.srvNow() === null, String(P.srvNow()));
check("...but the ratio it helped measure is untouched -- that is what a "
  + "pocketed phone runs on", P.srvRatio === ratioKept, ppm(P.srvRatio));

// ...and the winner is carried forward at that ratio while it is still valid,
// so an offset twenty seconds old is exact rather than twenty seconds of
// crystal difference out.
reset({ rate: 1 + 100e-6, rtt: 4 });
sample(P.RATIO_SPAN);
state.clock += 20000;
check("a good offset is carried forward at the measured ratio",
  Math.abs(offMs() - trueOffMs()) < 1,
  "off by " + (offMs() - trueOffMs()).toFixed(2) + " ms after 20 s");

// --- a daemon that restarted ----------------------------------------------

// monotonic() counts from an arbitrary origin and a restart picks a new one.
// Nothing the page could measure would say so, which is why the daemon does.
reset({ rtt: 4 });
sample(60);
check("the page is following a run of the daemon", P.srvSession === "9f3c1a2b");
srv.base = 12.5;                      // a fresh boot's monotonic clock
srv.from = state.clock / 1000;
srv.session = "0000ffff";
P.srvNextAt = 0;
P.timeSample();
run(1);
check("a restart is noticed rather than absorbed", P.srvSession === "0000ffff");
check("...and everything measured against the old origin is dropped",
  P.srvWin.length === 1 && P.srvRatio === 0 && P.tcBase === null,
  "win=" + P.srvWin.length + " ratio=" + ppm(P.srvRatio));
check("...including where the element thought it was",
  P.sndSeekPending === true);
sample(30);
check("...and the new origin is picked up",
  Math.abs(offMs() - trueOffMs()) < 1,
  "off by " + (offMs() - trueOffMs()).toFixed(2) + " ms");

// --- the steady cost ------------------------------------------------------

reset({ rtt: 4 });
sample(60);
check("a locked phone asks about once every TIME_EVERY seconds",
  net.hits.length <= 60 / P.TIME_EVERY + 1,
  net.hits.length + " requests in 60 s");

// A daemon that has gone away must not leave the page spinning on it.
reset({ rtt: 4 });
sample(10);
routes["/api/time"] = { rtt: 0, body: () => { throw new Error("gone"); } };
let threw = false;
try { sample(30); } catch (e) { threw = true; }
check("a route that fails does not take the page with it", !threw);

done();
