// The sound sheet: where the audio goes (mute or this phone), which language,
// how far to nudge it for this pair of headphones, and -- under ?debug -- the
// playback-parameter controls a listener can turn while standing there hearing
// the effect. It builds the sheet it drives, and the one prompt that lives
// outside it.
import { h, head, grip, setText, toggleClass } from "./dom";
import { row, langName } from "./sheet";
import type { AudioSession } from "./audio";
import type { Tunables, Tunable, StorageLike } from "./tunables";

export const SOUND_CSS = `
#sheetNote {
  color: var(--dim); font-size: 16px; line-height: 1.45; padding: 6px 14px 2px;
}

#syncRow { display: none; align-items: center; gap: 12px; padding: 4px 8px; }
#syncRow.on { display: flex; }
#syncRow button {
  width: 76px; min-height: 60px; border-radius: 16px; background: var(--card);
  font-size: 28px; font-weight: 700;
}
#syncVal { flex: 1; text-align: center; font-size: 19px; font-variant-numeric: tabular-nums; }
#syncHint { color: var(--dim); font-size: 14px; padding: 2px 14px 0; line-height: 1.4; }

/* Shown when iOS refuses to start audio without a fresh tap -- after a reload,
   or in Low Power Mode. Never a silent failure: somebody is sitting there
   wearing headphones that are not playing. */
#tapToListen {
  position: fixed; z-index: 25; left: 12px; right: 12px;
  bottom: calc(12px + env(safe-area-inset-bottom));
  min-height: 84px; border-radius: 20px; background: var(--accent);
  color: #06121f; font-size: 22px; font-weight: 700; letter-spacing: .4px;
  display: none;
}
#tapToListen.on { display: block; }

#syncDebug {
  color: var(--dim); font-size: 13px; padding: 10px 14px 0;
  font-variant-numeric: tabular-nums; display: none;
  /* Second line is the rolling counters, so the newline has to survive. */
  white-space: pre-line; line-height: 1.5;
}
#syncDebug.on { display: block; }

/* The controller's constants, editable on the phone that is hearing the
   problem. Only ever rendered under ?debug -- see paintTune(). */
#tuneHead, #tuneList, #tuneFoot { display: none; }
#tuneHead.on, #tuneList.on { display: block; }
#tuneFoot.on { display: flex; }
.tune { display: flex; align-items: center; gap: 8px; padding: 8px 14px 0; }
.tune .tname { flex: 1; font-size: 15px; min-width: 0; }
.tune .tval {
  min-width: 104px; text-align: right; font-size: 15px; color: var(--dim);
  font-variant-numeric: tabular-nums;
}
.tune .tval.changed { color: var(--accent); font-weight: 700; }
.tune button {
  width: 54px; min-height: 46px; border-radius: 13px; background: var(--card);
  font-size: 23px; font-weight: 700;
}
.thint { color: var(--dim); font-size: 12.5px; line-height: 1.4; padding: 3px 14px 6px; }
#tuneFoot { gap: 10px; padding: 8px 14px 2px; }
#tuneFoot button {
  flex: 1; min-height: 50px; border-radius: 14px; background: var(--card);
  font-size: 16px;
}
#tuneNote { color: var(--dim); font-size: 12.5px; line-height: 1.4; padding: 4px 14px 0; }
`;

export interface SoundSheetDeps {
  debug: boolean;
  libraryLangs: () => readonly string[];
  storage: StorageLike;
}

export class SoundSheet {
  /** The sheet, mounted by the composition root and shown by the manager. */
  readonly root: HTMLElement;
  /** The gesture prompt, which is fixed to the bottom of the page rather than
   * to the sheet: it has to be tappable while the sheet is shut. */
  readonly tapToListen: HTMLElement;
  private readonly doc: Document;
  private readonly destList: HTMLElement;
  private readonly langList: HTMLElement;
  private readonly note: HTMLElement;
  private readonly syncRow: HTMLElement;
  private readonly syncHead: HTMLElement;
  private readonly syncHint: HTMLElement;
  private readonly syncVal: HTMLElement;
  private readonly syncDebug: HTMLElement;
  private readonly tuneHead: HTMLElement;
  private readonly tuneList: HTMLElement;
  private readonly tuneFoot: HTMLElement;
  private readonly tuneNote: HTMLElement;

  constructor(
    doc: Document,
    private readonly audio: AudioSession,
    private readonly tunables: Tunables,
    private readonly deps: SoundSheetDeps,
  ) {
    this.doc = doc;
    this.destList = h(doc, "div", { id: "destList" });
    this.langList = h(doc, "div", { id: "langList" });
    this.note = h(doc, "div", { id: "sheetNote" });
    this.syncHead = head(doc, "Sync", "syncHead");
    this.syncVal = h(doc, "span", { id: "syncVal", text: "0 ms" });
    this.syncRow = h(doc, "div", { id: "syncRow" }, [
      h(doc, "button", {
        id: "syncBack",
        aria: "Sound earlier",
        text: "−",
        onTap: () => this.audio.nudgeTrim(-25),
      }),
      this.syncVal,
      h(doc, "button", {
        id: "syncFwd",
        aria: "Sound later",
        text: "+",
        onTap: () => this.audio.nudgeTrim(25),
      }),
    ]);
    this.syncHint = h(doc, "div", {
      id: "syncHint",
      text: "Nudge this if the words land before or after the mouths.",
    });
    this.syncDebug = h(doc, "div", { id: "syncDebug" });
    this.tuneHead = head(doc, "Playback parameters", "tuneHead");
    this.tuneList = h(doc, "div", { id: "tuneList" });
    this.tuneFoot = h(doc, "div", { id: "tuneFoot" }, [
      h(doc, "button", {
        id: "tuneReset",
        type: "button",
        text: "Reset to shipped",
        onTap: () => {
          this.tunables.reset();
          this.tunables.save(this.deps.storage);
          this.paint();
        },
      }),
    ]);
    this.tuneNote = h(doc, "div", { id: "tuneNote" });

    this.root = h(
      doc,
      "div",
      {
        id: "sheet",
        class: "sheet",
        role: "dialog",
        modal: true,
        aria: "Sound",
      },
      [
        grip(doc),
        head(doc, "Sound"),
        this.destList,
        head(doc, "Language"),
        this.langList,
        this.note,
        this.syncHead,
        this.syncRow,
        this.syncHint,
        this.syncDebug,
        this.tuneHead,
        this.tuneList,
        this.tuneFoot,
        this.tuneNote,
      ],
    );

    this.tapToListen = h(doc, "button", {
      id: "tapToListen",
      text: "TAP TO LISTEN",
      onTap: () => {
        toggleClass(this.tapToListen, "on", false);
        this.audio.acceptGesture();
      },
    });
  }

  paint(): void {
    const audio = this.audio;
    const listening = audio.listening;

    this.destList.textContent = "";
    this.destList.appendChild(
      row(this.doc, "Mute", "", !listening, () => audio.setDest("mute")),
    );
    this.destList.appendChild(
      row(this.doc, "This device", "Headphones on this phone", listening, () =>
        audio.enableDevice(),
      ),
    );

    this.langList.textContent = "";
    setText(this.note, "");
    const tracks = audio.availableTracks;
    if (!audio.phoneAudioEnabled) {
      setText(this.note, "Headphone sound is switched off on the player.");
    } else if (audio.filmReady && tracks.length) {
      for (const track of tracks) {
        const sub = (track.channels ?? 0) > 2 ? track.channels + " channels" : "";
        this.langList.appendChild(
          row(this.doc, langName(track.lang, track.title), sub, track.n === audio.currentTrack, () =>
            audio.setTrack(track),
          ),
        );
      }
    } else if (audio.filmReady) {
      setText(
        this.note,
        "This film hasn't been prepared for headphones yet. Run playstick-prep.py over the library.",
      );
    } else if (this.deps.libraryLangs().length) {
      for (const code of this.deps.libraryLangs()) {
        this.langList.appendChild(
          row(this.doc, langName(code), "", code === audio.language, () =>
            audio.preferLanguage(code),
          ),
        );
      }
      setText(this.note, "Pick one now and it will be used when a film starts.");
    } else {
      setText(this.note, "Start a film and its languages will appear here.");
    }

    const showSync = listening;
    toggleClass(this.syncRow, "on", showSync);
    this.syncHead.style.display = showSync ? "" : "none";
    this.syncHint.style.display = showSync ? "" : "none";
    setText(this.syncVal, (audio.trim > 0 ? "+" : "") + audio.trim + " ms");
    toggleClass(this.syncDebug, "on", this.deps.debug);
    this.paintTune();
  }

  private paintTune(): void {
    const on = this.deps.debug;
    toggleClass(this.tuneHead, "on", on);
    toggleClass(this.tuneList, "on", on);
    toggleClass(this.tuneFoot, "on", on);
    if (!on) {
      return;
    }
    this.tuneList.textContent = "";
    let changed = 0;
    for (const t of this.tunables.items) {
      const doc = this.doc;
      this.tuneList.appendChild(
        h(doc, "div", { class: "tune" }, [
          this.tuneButton("\u2212", "Decrease " + t.label, () => this.stepTune(t, -1)),
          h(doc, "span", { class: "tname", text: t.label }),
          h(doc, "span", {
            class: this.tunables.changed(t) ? "tval changed" : "tval",
            text: this.tunables.text(t),
          }),
          this.tuneButton("+", "Increase " + t.label, () => this.stepTune(t, 1)),
        ]),
      );
      this.tuneList.appendChild(
        h(doc, "div", {
          class: "thint",
          text:
            t.name + " \u00b7 shipped " + this.tunables.shippedText(t) + " \u00b7 " + t.hint,
        }),
      );
      if (this.tunables.changed(t)) {
        changed++;
      }
    }
    setText(
      this.tuneNote,
      changed
        ? changed + " changed. These apply to this phone only, while ?debug is in the URL."
        : "Everything is at the shipped value. Changes apply immediately, to this phone only.",
    );
  }

  private stepTune(t: Tunable, direction: number): void {
    this.tunables.step(t, direction);
    this.tunables.save(this.deps.storage);
    this.paintTune();
  }

  private tuneButton(label: string, aria: string, onTap: () => void): HTMLElement {
    return h(this.doc, "button", { type: "button", text: label, aria, onTap });
  }
}
