// Loads the real playstick-ui.html script under a stub DOM.
//
// The point of doing it this way rather than with a headless browser: the
// thing under test is a controller -- a loop with a clock, a plant and a
// feedback path -- and a controller is only testable if time is a variable you
// hold. Here it is `clock`, advanced a tick at a time by the driver. Nothing
// else about the page is faked: the script that runs is the one that ships.
//
// Requires node, which the Python suite deliberately does not. See tests/js/run.sh.
const fs = require("fs");

class FakeEl {
  constructor(id) {
    this.id = id; this.value = ""; this.style = {};
    this.children = []; this.dataset = {}; this.innerHTML = "";
    this._ev = {}; this.attrs = {}; this._text = "";
    this.classList = {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    };
  }
  // Assigning textContent replaces everything under the node, children
  // included -- which is how the page clears a list before repainting it. A
  // stub that only stored the string would let children pile up invisibly and
  // would hide the real leak if one ever appeared.
  get textContent() { return this._text; }
  set textContent(v) { this._text = v; this.children = []; }

  // className and classList are two views of one thing in a real DOM. The page
  // writes whichever is shorter at each site, so a stub where they are separate
  // reports "no class" for half the elements it is asked about.
  get className() { return [...this.classList._s].join(" "); }
  set className(v) {
    this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean));
  }

  // Listeners are kept rather than dropped so a test can tap a control the way
  // a listener would, which is the only way to reach a handler the page
  // attached at load.
  addEventListener(type, fn) {
    (this._ev[type] = this._ev[type] || []).push(fn);
  }
  removeEventListener() {}
  appendChild(c) { this.children.push(c); return c; }
  removeChild() {} getAttribute() { return null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector() { return new FakeEl("q"); } querySelectorAll() { return []; }
  focus() {} blur() {} scrollIntoView() {}
  click() { (this._ev.click || []).forEach((fn) => fn({ preventDefault() {} })); }
}

// play()/pause() are recorded rather than merely obeyed: several of the checks
// are about whether the element was left running in a state where the page
// could not say where to put it.
const acts = [];

// Every location.reload() the page asked for, stamped with the clock it asked
// at. Cleared by install().
const reloads = [];

class FakeAudio {
  constructor() {
    this._src = null; this.paused = true; this.currentTime = 0;
    this.duration = 7200; this.playbackRate = 1; this.readyState = 4;
    this.preload = ""; this.preservesPitch = false;
    this._ranges = [[0, 300]];
    this.buffered = {
      length: 1,
      start: (i) => this._ranges[i][0],
      end: (i) => this._ranges[i][1],
    };
  }
  setBuffered(ranges) { this._ranges = ranges; this.buffered.length = ranges.length; }
  addEventListener() {} removeEventListener() {}
  play() { acts.push("play"); this.paused = false; return Promise.resolve(); }
  pause() { acts.push("pause"); this.paused = true; }
  load() {}
  setAttribute(k, v) { if (k === "src") this._src = v; }
  getAttribute(k) { return k === "src" ? this._src : null; }
  removeAttribute(k) { if (k === "src") this._src = null; }
  get src() { return this._src; }
  set src(v) { this._src = v; }
}

const state = { clock: 1000 };

// Timers the page asked for. Nothing fires on its own: run() advances the
// clock and fires whatever came due, so a test that never calls it sees
// exactly the behaviour these tests have always seen.
const timers = [];
let timerId = 0;

// Routes the page is allowed to fetch. A path with no entry NEVER SETTLES,
// which is what every test here relied on before there were any -- the driver
// calls audioStatus() itself rather than letting a fake network decide when
// things happen. /api/time is the exception, and has to be: the page's whole
// clock model is built out of that round trip, so the round trip has to be
// something a test can set and then change.
const routes = {};

// A resolved promise that runs its callbacks inline. Real promises defer to a
// microtask, which would mean every check in every driver had to become async
// to observe a fetch the page made two lines earlier. Nothing in the page
// depends on that deferral, and a test that reads like a straight line is
// worth more here than one that models the event loop.
function settled(v) {
  return {
    then(fn) {
      const out = fn ? fn(v) : v;
      return out && typeof out.then === "function" ? out : settled(out);
    },
    catch() { return this; },
  };
}

// ...and its opposite, for a route that could not answer. A real fetch REJECTS
// rather than throwing, and code written against a harness that threw would be
// wrong in the one case it exists to survive: the daemon going away.
function rejected(err) {
  return {
    then() { return this; },
    catch(fn) { return settled(fn ? fn(err) : undefined); },
  };
}

function run(ms) {
  const end = state.clock + ms;
  // Re-scanned after every callback, because a callback may schedule the next
  // link in its own chain -- which is exactly what the /api/time burst does.
  for (;;) {
    let next = null;
    for (const t of timers) {
      if (t.at <= end && (next === null || t.at < next.at)) { next = t; }
    }
    if (next === null) { break; }
    timers.splice(timers.indexOf(next), 1);
    state.clock = Math.max(state.clock, next.at);
    next.fn();
  }
  state.clock = end;
}

function install(search) {
  const els = {};
  global.document = {
    hidden: false,
    getElementById: (id) => (els[id] = els[id] || new FakeEl(id)),
    createElement: (t) => new FakeEl(t),
    querySelector: () => new FakeEl("q"), querySelectorAll: () => [],
    addEventListener: () => {}, body: new FakeEl("body"),
  };
  // Counted rather than obeyed. A reload is the one thing the page can do
  // that ends the page, so a test that could only observe it by ceasing to
  // exist could not observe it at all.
  reloads.length = 0;
  global.location = {
    search: search, href: "http://stick/" + search,
    reload: () => { reloads.push(state.clock); },
  };
  global.localStorage = {
    _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
    setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
  };
  global.performance = { now: () => state.clock };
  global.navigator = { userAgent: "harness" };
  timers.length = 0;
  Object.keys(routes).forEach((k) => { delete routes[k]; });
  global.fetch = (url) => {
    const route = routes[String(url).split("?")[0]];
    if (!route) { return new Promise(() => {}); }
    // The clock moves across the request, because the page is timing it, and
    // the body is built halfway through -- a symmetric path, where the answer
    // really was true at the midpoint the page assumes. A route that wants a
    // one-sided path moves the clock itself inside body().
    const rtt = route.rtt || 0;
    state.clock += rtt / 2;
    let body;
    try {
      body = route.body(state);
    } catch (err) {
      state.clock += rtt / 2;
      return rejected(err);
    }
    state.clock += rtt / 2;
    return settled({ json: () => settled(body) });
  };
  global.setTimeout = (fn, ms) => {
    timers.push({ id: ++timerId, at: state.clock + (ms || 0), fn });
    return timerId;
  };
  // Left as a no-op on purpose: the correction loop is driven by the driver
  // calling sndCorrect(), which is the only way a controller is testable.
  global.setInterval = () => 0;
  global.clearTimeout = (id) => {
    const i = timers.findIndex((t) => t.id === id);
    if (i >= 0) { timers.splice(i, 1); }
  };
  global.clearInterval = () => {};
  global.requestAnimationFrame = () => 0;
  global.addEventListener = () => {};
  global.matchMedia = () => ({ matches: false, addEventListener() {} });
  global.Audio = FakeAudio;
  global.MediaMetadata = class {};
}

// The page is strict-mode, so its `var`s stay inside the eval rather than
// landing on globalThis. Accessors are appended INSIDE that scope, which keeps
// the script running exactly as it does in a browser and still lets a driver
// read and write the state the controller is built from.
function load(path, vars, fns) {
  const html = fs.readFileSync(path, "utf8");
  const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1]).join("\n");
  const exportSrc = "globalThis.P = {" +
    vars.map((v) => `get ${v}(){return ${v};}, set ${v}(x){${v}=x;}`).join(",") +
    ", " + fns.join(", ") + " };";
  (0, eval)(script + "\n" + exportSrc);
  return globalThis.P;
}

const fail = [];
function check(name, ok, detail) {
  console.log((ok ? "ok   " : "FAIL ") + name + (detail ? "  " + detail : ""));
  if (!ok) fail.push(name);
}
function done() {
  console.log(fail.length ? "\nFAILED: " + fail.join(", ") : "\nall checks passed");
  process.exit(fail.length ? 1 : 0);
}

module.exports = { install, load, check, done, run, acts, reloads, routes,
  state, FakeEl, FakeAudio };
