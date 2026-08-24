// Composition root. Every module above is pure or takes its browser touchpoints
// as injected seams; this one file owns the real ones -- document, location,
// localStorage, fetch, the clock, the timers and the one <audio> element -- and
// wires them together, then hangs the page the views built on the body. It is
// the only file that cannot be unit-tested headless, which is why it holds no
// logic worth testing: just construction and cabling.
import { BUILD, ReloadPolicy, stamped } from "./build";
import { CLOCK, SYNC, TIMING } from "./config";
import { ServerClock } from "./clock";
import { FilmClock } from "./timecode";
import { SyncController } from "./sync";
import { LibraryModel } from "./library";
import { ApiClient } from "./net";
import { StatusPresenter } from "./presenter";
import { LibraryView } from "./library-view";
import { Controls } from "./controls";
import { AudioSession, type AudioElement } from "./audio";
import { Tunables } from "./tunables";
import { Telemetry } from "./telemetry";
import { SoundSheet } from "./sound-sheet";
import { FilterSheet } from "./filter-sheet";
import { SheetManager } from "./sheet";
import { TimeDriver } from "./time-driver";
import { Poller } from "./poller";
import { toggleClass } from "./dom";
import { installStyles } from "./styles";
import type { Status } from "./types";

function boot(): void {
  const doc = document;
  // Before anything is built, so the first element to reach the body is already
  // styled.
  installStyles(doc);
  const debug = location.search.indexOf("debug") >= 0;
  const nowSec = () => performance.now() / 1000;
  const stamp = (path: string) => stamped(path);

  // --- the computational cores ------------------------------------------
  const clock = new ServerClock(CLOCK);
  const film = new FilmClock(clock);
  const sync = new SyncController(SYNC);
  const model = new LibraryModel();
  const api = new ApiClient((url, init) => fetch(url, init));

  // --- the one audio element, wrapped to the narrow interface -----------
  const media = new Audio();
  media.preload = "auto";
  // Rate correction is a pitch shift, not a time stretch: inaudible at the
  // fraction of a percent this runs at, and free of the artefacts stretching
  // speech produces.
  media.preservesPitch = false;
  const audioEl: AudioElement = {
    get currentTime() {
      return media.currentTime;
    },
    set currentTime(v: number) {
      media.currentTime = v;
    },
    get duration() {
      return media.duration || 0;
    },
    get playbackRate() {
      return media.playbackRate;
    },
    set playbackRate(v: number) {
      media.playbackRate = v;
    },
    get paused() {
      return media.paused;
    },
    play: () => media.play(),
    pause: () => media.pause(),
    load: () => media.load(),
    getSrc: () => media.getAttribute("src"),
    setSrc: (s: string) => media.setAttribute("src", s),
    clearSrc: () => media.removeAttribute("src"),
  };

  // --- views and presenter ----------------------------------------------
  // Built bottom-up: the sheets first, then the chrome that opens them, then
  // the grid that borrows the funnel, then the presenter that switches between
  // the three views. The cross-references that point the other way are all
  // callbacks, which nothing fires until the page is up.
  const reload = new ReloadPolicy({ reload: () => location.reload() });

  const audio = new AudioSession({
    el: audioEl,
    clock,
    film,
    sync,
    stamp,
    storage: localStorage,
    now: nowSec,
    onChange: () => {
      paintAudioBtn();
      if (sheets.soundOpen) {
        soundSheet.paint();
      }
    },
  });

  const tunables = new Tunables(SYNC, CLOCK, TIMING, () => armTick());
  tunables.load(localStorage, debug);
  const telemetry = new Telemetry();

  const soundSheet = new SoundSheet(doc, audio, tunables, {
    debug,
    libraryLangs: () => model.facets.langs,
    storage: localStorage,
  });
  const filterSheet = new FilterSheet(doc, model, () => libraryView.render());
  const sheets = new SheetManager(doc, {
    sound: soundSheet.root,
    filter: filterSheet.root,
    onSoundOpen: () => soundSheet.paint(),
    onFilterOpen: () => filterSheet.paint(),
  });

  const libraryView = new LibraryView(doc, model, stamp, (id) => play(id), sheets.filterBtn);
  const presenter = new StatusPresenter(doc, {
    reload,
    stamp,
    now: Date.now,
    library: libraryView.root,
    grid: libraryView.gridEl,
    filterBtn: sheets.filterBtn,
  });

  new Controls(
    {
      toggle: presenter.playing.toggle,
      stop: presenter.playing.stop,
      cancel: presenter.preparing.cancel,
      volDown: presenter.playing.volDown,
      volUp: presenter.playing.volUp,
    },
    api,
    {
      stateOf: () => presenter.state,
      apply: (s) => onStatus(s),
      toLibrary: () => presenter.views.show("library"),
    },
  );

  // --- the page ----------------------------------------------------------
  // Document order as it was in the template: the fixed chrome first, then the
  // scrim and sheets that stack over everything, then the three views.
  for (const el of [
    presenter.banner.root,
    sheets.audioBtn,
    sheets.filterBtn,
    soundSheet.tapToListen,
    sheets.scrim,
    soundSheet.root,
    filterSheet.root,
    libraryView.root,
    presenter.preparing.root,
    presenter.playing.root,
  ]) {
    doc.body.appendChild(el);
  }

  const timeDriver = new TimeDriver(api, clock, TIMING, {
    now: nowSec,
    schedule: (fn, ms) => window.setTimeout(fn, ms),
    onReset: () => sync.replace(),
  });

  const poller = new Poller(api, {
    now: () => performance.now(),
    schedule: (fn, ms) => window.setTimeout(fn, ms),
    cancel: (id) => window.clearTimeout(id),
    hidden: () => doc.hidden,
    stateOf: () => presenter.state,
    onStatus: (s) => onStatus(s),
    onOutage: () => {
      presenter.views.show("library");
      presenter.banner.show("Connecting to Playstick...\u2026");
    },
    header: () => (debug ? telemetry.build(snapshot()) : undefined),
  });

  // --- the loop that ties status to everything --------------------------
  let libraryFetchedAt = 0;
  let buffering = 0;
  let waits = 0;

  function onStatus(s: Status): void {
    if (s.buffering) {
      buffering++;
    }
    presenter.apply(s);
    audio.onStatus(s);
    const onGrid =
      s.state !== "playing" && s.state !== "paused" && s.state !== "preparing";
    if (onGrid && Date.now() - libraryFetchedAt > 10000) {
      loadLibrary();
    }
  }

  function loadLibrary(): void {
    void api
      .library()
      .then((d) => {
        libraryFetchedAt = Date.now();
        model.setItems(d.items);
        libraryView.render();
        if (sheets.soundOpen) {
          soundSheet.paint();
        }
        if (sheets.filterOpen) {
          filterSheet.paint();
        }
        if (d.available === false) {
          libraryView.unavailable();
        }
      })
      .catch(() => {
        /* the status poll reports the outage */
      });
  }

  function play(id: string): void {
    const item = model.all.find((i) => i.id === id);
    if (!item) {
      return;
    }
    // Drawn before the POST answers, with the poster the grid already has, so
    // the tap makes a picture of the right film immediately.
    presenter.preparing.begin(item.title, id);
    presenter.views.show("preparing");
    void api
      .play(id)
      .then((res) => {
        if (!res.ok) {
          presenter.views.show("library");
          presenter.banner.show(res.data.error || "That didn't work.", 6000);
        } else {
          onStatus(res.data as unknown as Status);
        }
      })
      .catch(() => presenter.views.show("library"));
  }

  // --- the audio icon ---------------------------------------------------
  const ICON_ON =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<path d="M4 9v6h4l5 4V5L8 9H4z"/>' +
    '<path d="M16.5 8.5a5 5 0 0 1 0 7" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>';
  const ICON_OFF =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<path d="M4 9v6h4l5 4V5L8 9H4z"/>' +
    '<path d="M16 9.5l5 5M21 9.5l-5 5" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>';
  const audioBtn = sheets.audioBtn;
  function paintAudioBtn(): void {
    toggleClass(audioBtn, "on", audio.phoneAudioEnabled);
    toggleClass(audioBtn, "live", audio.playing);
    audioBtn.innerHTML = audio.listening ? ICON_ON : ICON_OFF;
  }

  // --- the correction tick ----------------------------------------------
  let tickTimer: number | null = null;
  function armTick(): void {
    if (tickTimer !== null) {
      window.clearInterval(tickTimer);
    }
    tickTimer = window.setInterval(() => {
      timeDriver.tick();
      audio.correct(nowSec());
      if (debug) {
        telemetry.recordErrMs(sync.err * 1000);
      }
    }, SYNC.tick * 1000);
  }

  // The NAS lost the file under a playing track; and the two counters that tell
  // a starving element from a self-inflicted glitch apart.
  media.addEventListener("error", () => audio.onElementError());
  media.addEventListener("waiting", () => waits++);
  media.addEventListener("stalled", () => waits++);

  document.addEventListener("visibilitychange", () => {
    if (doc.hidden) {
      if (audio.playing) {
        sync.coast(audioEl, nowSec(), clock.ratio);
      }
    } else {
      timeDriver.burst(TIMING.burst);
      poller.poll();
    }
  });

  function snapshot() {
    const srvNow = clock.now(nowSec());
    const rtt = clock.bestRtt();
    return {
      now: nowSec(),
      listening: audio.listening,
      hasSrc: !!audioEl.getSrc(),
      paused: media.paused,
      hidden: doc.hidden,
      currentTime: media.currentTime,
      errMs: sync.err * 1000,
      ratePpm: (sync.rate - 1) * 1e6,
      ratioPpm: clock.ratio * 1e6,
      driftPpm: sync.integrator * 1e6,
      offsetMs: srvNow === null ? null : (srvNow - nowSec()) * 1000,
      rttMs: rtt === null ? null : rtt * 1000,
      samples: clock.samples,
      epoch: film.epoch,
      writes: sync.rateWrites,
      seeks: sync.seeks,
      waits,
      buffering,
      digest: tunables.digest(),
    };
  }

  // --- start -------------------------------------------------------------
  paintAudioBtn();
  armTick();
  loadLibrary();
  timeDriver.burst(TIMING.burst);
  poller.poll();

  // The stamp the daemon rewrites lives on for the tests that assert it ships.
  doc.documentElement.dataset["build"] = BUILD;
}

boot();
