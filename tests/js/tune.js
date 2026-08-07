// The debug sheet's playback-parameter controls.
//
// These edit the constants a listener is standing there hearing the effect of,
// so the things worth holding are: a tap really reaches the controller, the
// value cannot leave its range, it survives a reload, it is recorded in the
// telemetry, and none of it happens at all without ?debug.
const { install, load, check, done, state } = require("./page.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["TUNABLES", "SEEK_LIMIT", "KP", "KI", "RATE_EPS", "RATE_LIMIT",
  "ERR_LP", "TICK", "STALL", "TIME_EVERY", "OFF_MAX_AGE", "RATIO_SPAN",
  "debugSync", "sndDest", "snd", "sndErr", "sndRateSet", "srvBest", "srvWin"];
const FNS = ["tuneLoad", "tuneSave", "tuneStep", "tuneDigest", "tuneValue",
  "tuneText", "tuneApply", "paintTune", "syncTelemetry"];

install("?debug");
const P = load(PAGE, VARS, FNS);
const byName = (n) => P.TUNABLES.filter((t) => t.name === n)[0];

check("every tunable declares what it needs to be rendered",
  P.TUNABLES.every((t) => t.code && t.name && t.label && t.hint &&
    typeof t.min === "number" && typeof t.max === "number" && t.step > 0 &&
    t.scale > 0 && typeof t.get === "function" && typeof t.set === "function"),
  P.TUNABLES.map((t) => t.code).join(","));
check("the short codes are unique",
  new Set(P.TUNABLES.map((t) => t.code)).size === P.TUNABLES.length);
check("every shipped value is inside its own range",
  P.TUNABLES.every((t) => {
    const v = t.def * t.scale;
    return v >= t.min - 1e-9 && v <= t.max + 1e-9;
  }),
  P.TUNABLES.filter((t) => t.def * t.scale < t.min || t.def * t.scale > t.max)
    .map((t) => t.name).join(",") || "all inside");

// 1. A tap reaches the constant the controller actually reads.
const seek = byName("SEEK_LIMIT");
check("SEEK_LIMIT is shown in the telemetry's units", P.tuneText(seek) === "250 ms",
  P.tuneText(seek));
P.tuneStep(seek, +1);
check("one tap moves it by one step", P.SEEK_LIMIT === 0.3, "" + P.SEEK_LIMIT);
P.tuneStep(seek, -1);
check("...and back", P.SEEK_LIMIT === 0.25, "" + P.SEEK_LIMIT);

// 2. Repeated taps must not accumulate float dust: 0.1+0.2 arithmetic on a
//    value the display rounds would drift away from what is shown.
const kp = byName("KP");
// Twenty, not forty: forty steps of 5 from a shipped 150 would hit the floor
// at 0 and the walk back could not return. Staying inside the range is what
// makes this a test of the arithmetic rather than of the clamp.
for (let i = 0; i < 20; i++) { P.tuneStep(kp, -1); }
for (let i = 0; i < 20; i++) { P.tuneStep(kp, +1); }
check("twenty taps each way land exactly back on the shipped value",
  P.KP === kp.def, P.KP + " vs " + kp.def);

// 3. The range is a fence, not a suggestion. A phone in somebody's hand cannot
//    be allowed to set a gain of zero-divided or a negative interval.
for (let i = 0; i < 200; i++) { P.tuneStep(kp, -1); }
check("cannot go below its minimum", P.KP * kp.scale >= kp.min - 1e-9,
  "" + P.KP * kp.scale);
for (let i = 0; i < 500; i++) { P.tuneStep(kp, +1); }
check("cannot go above its maximum", P.KP * kp.scale <= kp.max + 1e-9,
  "" + P.KP * kp.scale);
check("...and the value is still exactly on the step grid",
  Math.abs((P.KP * kp.scale) / kp.step - Math.round((P.KP * kp.scale) / kp.step)) < 1e-6,
  "" + P.KP * kp.scale);

// 4. TICK re-arms the loop. setInterval captures its period at the call, so a
//    changed TICK that did not re-arm would do nothing until a reload.
let armed = [];
global.setInterval = (fn, ms) => { armed.push(ms); return armed.length; };
global.clearInterval = () => {};
const tick = byName("TICK");
armed = [];
P.tuneStep(tick, +1);
check("changing TICK re-arms the correction loop",
  armed.length === 1 && armed[0] === P.TICK, JSON.stringify(armed));
P.tuneStep(tick, -1);

// 5. Recorded in the telemetry, or a capture taken mid-experiment is a capture
//    of an unknown build. Back to stock first: the range test above deliberately
//    left KP pegged at its maximum.
P.TUNABLES.forEach((t) => t.set(t.def));
check("a stock build sends an empty tun", P.tuneDigest() === "",
  P.tuneDigest());
P.tuneStep(seek, +2);
P.tuneStep(byName("RATE_EPS"), +4);
const digest = P.tuneDigest();
check("changed constants are listed by code", /sl:350/.test(digest) &&
  /re:300/.test(digest), digest);
check("...and only the changed ones", digest.split(",").length === 2, digest);
check("the digest survives the daemon's filter",
  /^[A-Za-z0-9=;:.,+_-]*$/.test(digest), digest);

P.sndDest = "device";
P.snd.src = "/api/audio/abc/0";
P.snd.paused = false;
P.srvBest = { off: 0, rtt: 0.004, at: state.clock / 1000 };
P.srvWin = [P.srvBest];
const blob = P.syncTelemetry();
check("tun rides in the telemetry header", /;tun=sl:350/.test(blob), blob);
check("the whole header still fits the daemon's 512-char cap",
  blob.length < 512, blob.length + " chars");

// 6. The controls themselves: one row per constant, each with a label, a
//    value, and the two taps that move it.
P.paintTune();
const list = global.document.getElementById("tuneList");
check("the sheet shows a control for every parameter",
  list.classList.contains("on") &&
  list.children.length === P.TUNABLES.length * 2,     // row + hint
  list.children.length + " children for " + P.TUNABLES.length + " parameters");
const first = list.children[0];
check("each row is minus / name / value / plus", first.children.length === 4 &&
  first.children[0].textContent === "−" && first.children[3].textContent === "+",
  first.children.map((c) => c.textContent).join("|"));
check("a changed value is marked as changed",
  first.children[2].classList.contains("changed"));
check("the buttons are labelled for a screen reader",
  first.children[0].attrs["aria-label"] === "Decrease " + P.TUNABLES[0].label,
  first.children[0].attrs["aria-label"]);
check("the hint names the constant and its shipped value",
  /SEEK_LIMIT · shipped 250 ms/.test(list.children[1].textContent),
  list.children[1].textContent.slice(0, 40));

// A tap on a real control, through the handler the page attached.
first.children[3].click();
check("tapping + on the rendered control moves the constant",
  P.SEEK_LIMIT === 0.4, "" + P.SEEK_LIMIT);

// Reset is the way back from a tuning that made things worse while somebody
// is standing there wearing the headphones.
global.document.getElementById("tuneReset").click();
check("reset restores every shipped value",
  P.TUNABLES.every((t) => t.get() === t.def) && P.tuneDigest() === "");
check("...and clears the saved override",
  global.localStorage.getItem("ps.snd.tune") === null,
  "" + global.localStorage.getItem("ps.snd.tune"));

// Put one back for the reload checks below.
P.tuneStep(byName("SEEK_LIMIT"), +2);

// 7. It survives a reload -- the phone doing the listening is also the phone
//    that reloads whenever iOS feels like it.
const stored = global.localStorage.getItem("ps.snd.tune");
check("changes are persisted", stored && JSON.parse(stored).SEEK_LIMIT === 350,
  stored);

const saved = global.localStorage._d;
install("?debug");
global.localStorage._d = saved;
const P2 = load(PAGE, VARS, FNS);
check("a reload with ?debug restores them", P2.SEEK_LIMIT === 0.35,
  "" + P2.SEEK_LIMIT);

// 7. And is confined to debug. A number tuned during one film must not follow
//    the next listener around invisibly.
install("");
global.localStorage._d = saved;
const P3 = load(PAGE, VARS, FNS);
check("a reload WITHOUT ?debug ignores them", P3.SEEK_LIMIT === 0.25,
  "" + P3.SEEK_LIMIT);
check("...and the controls are not rendered",
  (P3.paintTune(), global.document.getElementById("tuneList")
    .classList.contains("on") === false));
check("...and the shipped default is what it reports as default",
  P3.TUNABLES.every((t) => t.def === t.get()));

done();
