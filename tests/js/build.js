// Noticing that the page itself has been replaced.
//
// A deploy rewrites ui.html on the stick and restarts the daemon. Every phone
// in the house has the old page open and keeps polling it quite happily, so
// nothing about that deploy reaches them -- the page they are running is the
// one they fetched days ago, and the only thing that would replace it is a
// person deciding to refresh. Children do not refresh. Adults do not think to.
//
// So the page compares the build it was stamped with against the build the
// daemon reports, and reloads when they differ. The whole risk in that is
// WHEN: a reload is a page that forgets which film it is following and drops
// the audio element out of somebody's ears, so the checks below are mostly
// about the moments it must not pick.
//
// The script under test is the one that ships -- see tests/js/page.js.
const { install, load, check, done, reloads } = require("./page.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["state", "BUILD", "reloading", "library", "prepArtId",
  "bannerHold", "snd", "sndTracks", "sndFilmId"];
const FNS = ["apply", "checkBuild", "stamped", "sndLoad"];

install("");
const P = load(PAGE, VARS, FNS);

// What the daemon stamps into the served page. The literal in the file is a
// placeholder; a page in a browser has always been through http.py.
const HERE = "a1b2c3d4e5f6";
const THERE = "0f0f0f0f0f0f";

function reset() {
  P.BUILD = HERE;
  P.reloading = false;
  P.state = "idle";
  P.bannerHold = 0;
  reloads.length = 0;
}

function status(over) {
  return Object.assign({
    state: "idle", build: HERE, id: "", title: "",
    position: 0, position_valid: false, buffering: false, duration: 0,
    volume: null, audio: false, phone_audio: true, tracks: [],
    thumbs_pending: 0, prepare: null, notice: "",
    projector: { model: "", power: "unknown", fault: "" },
  }, over || {});
}

// --- the placeholder ------------------------------------------------------

const fs = require("fs");
const html = fs.readFileSync(PAGE, "utf8");

check("the shipped page carries the stamp the daemon rewrites",
  /var BUILD = "__PLAYSTICK_BUILD__";/.test(html));
check("...exactly once, so a rewrite cannot leave half the page disagreeing",
  (html.match(/__PLAYSTICK_BUILD__/g) || []).length === 1,
  (html.match(/__PLAYSTICK_BUILD__/g) || []).length);

// --- the things a browser is allowed to keep ------------------------------

// Reloading the page does not empty the image or media cache, so the two
// resources that cache hard -- a poster for up to a year, a soundtrack for an
// hour -- need the build in their URL or a deploy never reaches them.

reset();
check("a cached URL carries the build",
  P.stamped("/api/thumb/abc") === "/api/thumb/abc?v=" + HERE,
  P.stamped("/api/thumb/abc"));

P.BUILD = THERE;
check("...so a new build is a new URL",
  P.stamped("/api/thumb/abc") === "/api/thumb/abc?v=" + THERE,
  P.stamped("/api/thumb/abc"));

// The guard that matters, because these URLs are built at six sites and the
// seventh is the one somebody adds later: every literal poster or soundtrack
// path in the page has to be inside a stamped() call.
for (const route of ["thumb", "audio"]) {
  const all = (html.match(new RegExp('"/api/' + route + '/', "g")) || []).length;
  const wrapped =
    (html.match(new RegExp('stamped\\("/api/' + route + '/', "g")) || []).length;
  check("every " + route + " URL the page builds is stamped",
    all > 0 && all === wrapped, wrapped + " of " + all);
}

reset();
P.sndFilmId = "0123456789abcdef";
P.sndTracks = [{ n: 0, lang: "jpn", title: "Japanese", channels: 2,
  default: true, offset: 0 }];
P.sndLoad(0);
check("a soundtrack is fetched from a stamped URL",
  P.snd.src === "/api/audio/0123456789abcdef/0?v=" + HERE, P.snd.src);

// --- the steady state -----------------------------------------------------

reset();
P.apply(status());
check("the build it is already running is not a reason to reload",
  reloads.length === 0, reloads.length);

reset();
P.apply(status({ state: "playing", duration: 100, position: 10 }));
P.apply(status({ state: "paused", duration: 100, position: 10 }));
P.apply(status({ state: "airplay" }));
P.apply(status({ state: "unavailable" }));
check("...in any state the page can be in", reloads.length === 0,
  reloads.length);

// --- a deploy landed ------------------------------------------------------

reset();
P.apply(status({ build: THERE }));
check("a page the daemon no longer serves reloads itself",
  reloads.length === 1, reloads.length);

reset();
P.apply(status({ state: "airplay", build: THERE }));
check("...while somebody else is mirroring, which this page is not part of",
  reloads.length === 1, reloads.length);

reset();
P.apply(status({ state: "unavailable", build: THERE }));
check("...and while the NAS is away, where an old page is no more use",
  reloads.length === 1, reloads.length);

// A rollback is a build change like any other. The page has no way to know
// which direction it went and no business having an opinion: what the daemon
// is serving is what every phone should be running.
reset();
P.BUILD = THERE;
P.apply(status({ build: HERE }));
check("a rolled-back deploy is picked up the same way", reloads.length === 1,
  reloads.length);

// --- but not in the middle of something -----------------------------------

reset();
P.apply(status({ state: "playing", build: THERE, duration: 100, position: 10 }));
check("a film playing is not interrupted to install a new page",
  reloads.length === 0, reloads.length);

reset();
P.apply(status({ state: "paused", build: THERE, duration: 100, position: 10 }));
check("...nor a film somebody paused to fetch a drink", reloads.length === 0,
  reloads.length);

reset();
P.apply(status({ state: "preparing", build: THERE, id: "abc", title: "Ponyo" }));
check("...nor a lamp somebody is already waiting on", reloads.length === 0,
  reloads.length);

// The deferral has to end, or it is just a slower way of never reloading.
// This is the sequence a deploy actually produces: the restart stops the film,
// so the next poll after it says idle.
reset();
P.apply(status({ state: "playing", build: THERE, duration: 100, position: 10 }));
check("a deferred reload is still pending after the film", reloads.length === 0,
  reloads.length);
P.apply(status({ state: "idle", build: THERE }));
check("...and happens the moment there is nothing to lose",
  reloads.length === 1, reloads.length);

// --- an older daemon ------------------------------------------------------

// Every field this payload has ever gained was additive, and this one is read
// by a page that may be older than it. The reverse must hold too: a page that
// knows about builds, talking to a daemon that does not, must not spin.
reset();
const old = status();
delete old.build;
P.apply(old);
P.apply(old);
check("a daemon that reports no build at all is not a mismatch",
  reloads.length === 0, reloads.length);

reset();
P.apply(status({ build: "" }));
check("...nor is an empty one, which is what an unreadable page reports",
  reloads.length === 0, reloads.length);

// --- once is enough -------------------------------------------------------

// location.reload() does not stop the page: the current turn runs to the end,
// polls already in flight still land, and in a browser several more seconds of
// them follow while the new document is fetched. Every one sees the same
// mismatch.
reset();
P.apply(status({ build: THERE }));
P.apply(status({ build: THERE }));
P.apply(status({ build: THERE }));
check("a navigation already under way is not asked for again",
  reloads.length === 1, reloads.length);

// ...and none of it stops apply() finishing the screen the child is looking at
// while the new document loads.
reset();
P.apply(status({ build: THERE, notice: "That film would not start." }));
check("the reload does not swallow the rest of the screen",
  document.getElementById("banner").classList.contains("on"));

done();
