// The audio element as a plant: what it costs to command, and what it loses
// when it is.
//
// Split out of clock.js so more than one driver can drive it -- sweep.js
// calibrates it against a capture, clock.js asserts against it. Everything
// here was in clock.js first and the semantics are unchanged; what is new is
// seekCost, and the per-tick instrumentation that lets a run be compared
// against a real phone's telemetry field by field.
//
// The one rule this file exists to enforce: A HARNESS THAT DOES NOT CHARGE FOR
// AN ACTUATOR CANNOT SEE A LOOP PAYING FOR ITS OWN CORRECTIONS. Both of this
// controller's actuators cost audio, and both costs are measured rather than
// chosen -- see the constants below for where each number comes from.

// `lag` deciles from the 2026-08-12 capture: the element's own clock shortfall
// per interval, which is the only telemetry field that measures the symptom
// rather than a suspected cause.
//
// It is BIMODAL -- a tight cluster at 145-170 ms and a second at 249-257 --
// and that shape is the whole mechanism. A constant seek cost cannot reproduce
// the device: below the threshold the loop settles at zero seeks, above it the
// loop collapses to one every tick, and the capture sat at 1.43/s, which is
// neither. Whether the two modes are two different physical costs or one cost
// beating against the 250 ms tick grid is what sweep.js is for.
const SEEK_LAG_DECILES =
  [0.152, 0.158, 0.160, 0.164, 0.164, 0.167, 0.170, 0.249, 0.251, 0.257];
const SEEK_LAG_MEDIAN = 0.164;

// Shortfall in one TICK that the page counts as a stall rather than as
// reporting jitter. Kept in step with STALL in playstick-ui.html: the sim has
// to count what the phone counts or the two cannot be compared.
const STALL = 0.03;

function makePlant(P, state) {
  const dac = {
    rate: 1,          // audio hardware clock against the page's. 60 ppm is ordinary
    quantum: 0,       // currentTime on decoded-frame boundaries; 21.3 ms for AAC-LC
    writeCost: 0.043, // audio lost to a playbackRate write re-arming AVPlayer
    seekCost: 0,      // ...and to a seek tearing the pipeline down and re-reading
    seekSpread: 1,    // ticks the seek's stall is spread across (see sweep.js)
  };

  let ctTrue = 0;
  let lastRate = 1;
  let stallDebt = 0;      // seconds of wall time owed before the clock moves again
  let stalledTotal = 0;   // ...and paid: audio a listener did not hear
  let seekSeed = 1;

  // Per-tick instrumentation, defined exactly as the page defines its own, so
  // a sim run and a phone capture are the same measurement.
  let telLag = 0;         // worst single-tick shortfall
  let telStalls = 0;      // ticks whose shortfall passed STALL
  let telTicks = 0;
  const lags = [];

  // Deterministic: a harness whose verdict moves between runs is not a harness.
  function draw() {
    seekSeed = (seekSeed * 1103515245 + 12345) & 0x7fffffff;
    return SEEK_LAG_DECILES[seekSeed % SEEK_LAG_DECILES.length];
  }

  function quantise(t) {
    return dac.quantum ? Math.floor(t / dac.quantum) * dac.quantum : t;
  }

  function tick(ms) {
    const dt = (ms === undefined ? 250 : ms) / 1000;
    // A currentTime that is not where this left it is the page having seeked.
    if (Math.abs(P.snd.currentTime - quantise(ctTrue)) > 1e-9) {
      ctTrue = P.snd.currentTime;
      if (dac.seekCost) {
        // The measured SHAPE scales with the commanded median, so a device
        // profile carries one number and keeps the distribution it was
        // measured with.
        //
        // max() rather than +=, and the difference matters more than it
        // reads. A seek issued while the pipeline is already coming back up
        // does not make it come up twice as slowly -- it re-arms once. Adding
        // the debts instead lets it run away without bound, which is how an
        // earlier version of this file reported losing 117% of the film: an
        // arithmetic impossibility that only showed up because `lost` was
        // printed rather than assumed.
        stallDebt = Math.max(stallDebt, draw() * (dac.seekCost / SEEK_LAG_MEDIAN));
      }
    }
    // The plant pays for being re-commanded. Read the ELEMENT rather than
    // sndRateSet: that variable is what the page believes it asked for, and a
    // setRate() that updated the command without reaching snd.playbackRate has
    // to be visible here rather than invisible by construction.
    if (P.snd.playbackRate !== lastRate) {
      lastRate = P.snd.playbackRate;
      if (!P.snd.paused) { ctTrue -= dac.writeCost; }
    }
    state.clock += dt * 1000;
    if (!P.snd.paused) {
      // Wall time the element spent re-arming buys no film. Spread over
      // seekSpread ticks rather than paid off greedily: a pipeline coming back
      // up is not a hard stop followed by full speed, and how the debt lands
      // against the tick grid is what decides whether the page sees one big
      // shortfall or two smaller ones.
      const payable = dac.seekSpread > 1 ? dt / dac.seekSpread : dt;
      const stalled = Math.min(stallDebt, payable, dt);
      stallDebt -= stalled;
      stalledTotal += stalled;
      ctTrue += (dt - stalled) * P.snd.playbackRate * dac.rate;

      telTicks++;
      if (stalled > telLag) { telLag = stalled; }
      if (stalled > STALL) { telStalls++; }
      lags.push(stalled);
    }
    P.snd.currentTime = quantise(ctTrue);
  }

  function reset() {
    dac.rate = 1;
    dac.quantum = 0;
    // Both costs off by default, and deliberately. Every section of clock.js
    // below 11 was written against a plant that actuates for free, and each is
    // testing something else -- placement, pausing, resuming, the crystal, the
    // staircase. Turning the costs on for all of them would move numbers that
    // were argued out one at a time.
    dac.writeCost = 0;
    dac.seekCost = 0;
    dac.seekSpread = 1;
    ctTrue = 0;
    lastRate = 1;
    stallDebt = 0;
    stalledTotal = 0;
    seekSeed = 1;
    telLag = 0;
    telStalls = 0;
    telTicks = 0;
    lags.length = 0;
  }

  function marks() {
    const secs = telTicks * 0.25;
    const sorted = lags.slice().sort((a, b) => a - b);
    const pct = (p) => sorted.length
      ? sorted[Math.min(sorted.length - 1, Math.floor(p / 100 * sorted.length))]
      : 0;
    return {
      secs: secs,
      seeks: P.sndSeeks,
      seeksPerSec: secs ? P.sndSeeks / secs : 0,
      // Only shortfalls past STALL count, exactly as the page counts them.
      stallsPerSec: secs ? telStalls / secs : 0,
      lagWorst: telLag,
      lagMedian: pct(50),
      lagP90: pct(90),
      lost: secs ? stalledTotal / secs : 0,
    };
  }

  return {
    dac: dac, tick: tick, quantise: quantise, reset: reset, marks: marks,
    get ct() { return ctTrue; },
    set ct(v) { ctTrue = v; },
    get stalled() { return stalledTotal; },
    // Clears the measurement window without touching the plant's state, so a
    // settle phase can run before anything is counted. stalledTotal has to go
    // with the tick count or `lost` is a ratio of two different windows --
    // which is exactly how this file once reported losing 117% of a film.
    zero: function () {
      telLag = 0; telStalls = 0; telTicks = 0; lags.length = 0; stalledTotal = 0;
    },
  };
}

module.exports = { makePlant, SEEK_LAG_DECILES, SEEK_LAG_MEDIAN, STALL };
