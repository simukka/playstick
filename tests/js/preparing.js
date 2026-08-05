// The view a child watches while a lamp warms up.
//
// This is the screen with the least going on and the most riding on it. For
// the better part of a minute it is the only evidence that anything is
// happening at all, and a child who concludes the appliance is broken acts on
// that conclusion -- they press things, or they fetch an adult, or they give
// up. So the things worth holding are: the tap produces a picture of the right
// film immediately, the words come from the server rather than from a second
// copy here that can drift, there is always a way out, and every way the
// attempt can end puts the child somewhere they can act.
//
// The script under test is the one that ships -- see tests/js/page.js.
const { install, load, check, done } = require("./page.js");

const PAGE = process.argv[2] ||
  __dirname + "/../../roles/player/files/playstick-ui.html";
const VARS = ["state", "library", "prepArtId", "busyUntil", "bannerHold",
  "thumbState", "tileImgs"];
const FNS = ["apply", "showView", "play", "showBanner", "renderLibrary",
  "guard"];

install("");
const P = load(PAGE, VARS, FNS);

const el = (id) => document.getElementById(id);

function shown() {
  return ["library", "preparing", "playing"]
    .filter((v) => el(v).classList.contains("on")).join(",");
}

function reset() {
  P.state = "idle";
  P.prepArtId = "";
  P.busyUntil = 0;
  P.bannerHold = 0;
  el("banner").classList.remove("on");
}

// A status payload as the daemon sends one. Every key the page reads is here,
// so a test that omits one is testing a payload the server never produces.
function status(over) {
  return Object.assign({
    state: "preparing", id: "abc", title: "Ponyo",
    position: 0, position_valid: false, buffering: false, duration: 0,
    volume: null, audio: false, phone_audio: true, tracks: [],
    thumbs_pending: 0, prepare: null, notice: "",
    projector: { model: "pt-ae4000", power: "standby", fault: "" },
  }, over || {});
}

// --- the tap -------------------------------------------------------------

reset();
P.play({ id: "abc", title: "Ponyo" });
check("a tap goes straight to the preparing view", shown() === "preparing",
  shown());
check("...showing the film that was tapped",
  el("prepTitle").textContent === "Ponyo", el("prepTitle").textContent);
check("...with its poster, from the grid the page already has",
  el("prepArt").src === "/api/thumb/abc", el("prepArt").src);
check("...and something to read before the first poll arrives",
  el("prepStep").textContent.length > 0, el("prepStep").textContent);

// --- the steps -----------------------------------------------------------

reset();
P.apply(status({
  prepare: { step: "warming", label: "Waiting for the lamp…", since: 12.4 },
}));
check("the step is shown in the server's words, verbatim",
  el("prepStep").textContent === "Waiting for the lamp…",
  el("prepStep").textContent);
check("the view stays put while the steps run", shown() === "preparing");

P.apply(status({
  prepare: { step: "starting", label: "Starting the movie…", since: 41.0 },
}));
check("...and follows the sequence", el("prepStep").textContent === "Starting the movie…",
  el("prepStep").textContent);

// A daemon that reports "preparing" with no step yet -- the window between
// begin() returning and the thread setting one -- must not blank the line.
P.apply(status({ prepare: null }));
check("a step that has not arrived yet still says something",
  el("prepStep").textContent.length > 0, el("prepStep").textContent);

// --- somebody else's phone ------------------------------------------------

reset();
P.apply(status({ id: "zzz", title: "Totoro",
  prepare: { step: "warming", label: "Waiting for the lamp…", since: 1 } }));
check("a film started elsewhere names itself here too",
  el("prepTitle").textContent === "Totoro", el("prepTitle").textContent);
check("...and brings its own poster", el("prepArt").src === "/api/thumb/zzz",
  el("prepArt").src);

const before = el("prepArt").src;
el("prepArt").src = "SENTINEL";
P.apply(status({ id: "zzz", title: "Totoro",
  prepare: { step: "input", label: "Pointing it at the movie…", since: 2 } }));
check("the poster is not refetched on every poll",
  el("prepArt").src === "SENTINEL", el("prepArt").src);
el("prepArt").src = before;

// --- getting out ----------------------------------------------------------

reset();
P.apply(status({ prepare: { step: "warming", label: "Waiting for the lamp…", since: 3 } }));
// Read from the shipped file rather than from the stub DOM, which never
// parses markup -- the wording on this button is a design decision and worth
// holding, and it exists nowhere the page's own script can see it.
const MARKUP = require("fs").readFileSync(PAGE, "utf8");
check("there is a way out of the wait",
  /<button id="prepCancel"[^>]*>Never mind<\/button>/.test(MARKUP));
check("...and it is not dressed as the red STOP",
  /#prepCancel\s*{[^}]*background:\s*var\(--card\)/.test(MARKUP));
el("prepCancel").click();
check("...and taking it goes back to the grid", shown() === "library", shown());

// --- how it can end -------------------------------------------------------

reset();
P.apply(status({ state: "playing", title: "Ponyo", duration: 100, position: 10 }));
check("a film that starts moves to the now-playing view",
  shown() === "playing", shown());

reset();
P.apply(status({ state: "idle", id: "", title: "",
  notice: "The projector is being used for AirPlay." }));
check("an attempt that gave up returns to the grid", shown() === "library",
  shown());
check("...and says why, which the POST could not",
  el("banner").textContent === "The projector is being used for AirPlay.",
  el("banner").textContent);
check("...visibly", el("banner").classList.contains("on"));

reset();
P.apply(status({ state: "idle", id: "", title: "", notice: "" }));
check("no notice, no banner", !el("banner").classList.contains("on"));

// --- a projector that could not be reached --------------------------------

reset();
P.apply(status({ state: "playing", title: "Ponyo", duration: 100, position: 10,
  projector: { model: "pt-ae4000", power: "unknown",
               fault: "I couldn't reach the projector." } }));
check("a film playing on an unreachable projector says so",
  el("banner").classList.contains("on") &&
  /projector/.test(el("banner").textContent), el("banner").textContent);

reset();
P.apply(status({ state: "preparing",
  prepare: { step: "warming", label: "Waiting for the lamp…", since: 3 },
  projector: { model: "pt-ae4000", power: "unknown",
               fault: "I couldn't reach the projector." } }));
check("...but not while the steps are still retrying it",
  !el("banner").classList.contains("on"), el("banner").textContent);

reset();
P.apply(status({ state: "playing", title: "Ponyo", duration: 100, position: 10 }));
check("a healthy projector shows no banner",
  !el("banner").classList.contains("on"), el("banner").textContent);

// --- the banner hold ------------------------------------------------------
//
// A refusal shown by play() used to last until the status poll a second later
// cleared it, so the one message explaining why nothing happened was the one
// nobody could read.

reset();
P.showBanner("Somebody else is using the projector.", 6000);
P.apply(status({ state: "idle", id: "", title: "", notice: "" }));
check("a deliberate message survives the next poll",
  el("banner").classList.contains("on"), el("banner").textContent);
P.bannerHold = 0;
P.apply(status({ state: "idle", id: "", title: "", notice: "" }));
check("...and goes once its time is up",
  !el("banner").classList.contains("on"));

// --- an older payload -----------------------------------------------------
//
// Every field this feature added is additive, and the daemon and the page are
// deployed by the same Ansible run but not necessarily in the same instant.

reset();
P.apply({ state: "playing", title: "Ponyo", position: 10, duration: 100,
  volume: null, audio: false, tracks: [] });
check("a status with none of the new keys still plays a film",
  shown() === "playing", shown());

done();
