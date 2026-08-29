// The subtitle sheet: which subtitles are on the picture, or none.
//
// Its own sheet rather than a section of the Sound sheet, and that is a
// statement about who a tap affects. Everything in the Sound sheet is about the
// phone holding it -- these headphones, this language, this trim -- and two
// people can answer it differently in the same room. Subtitles are on the
// projector everybody is looking at, so a tap here changes what somebody else
// sees. Filing them under "Sound" would have said the opposite.
import { h, head, grip, setText } from "./dom";
import { row, langName } from "./sheet";
import type { Status, Track } from "./types";

export const SUBS_CSS = `
#subsNote {
  color: var(--dim); font-size: 16px; line-height: 1.45; padding: 6px 14px 2px;
}
`;

export interface SubsSheetDeps {
  /** Pick a track, or 0 for none. Answers with the daemon's new status. */
  choose: (track: number) => void;
}

export class SubsSheet {
  /** The sheet, mounted by the composition root and shown by the manager. */
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly list: HTMLElement;
  private readonly note: HTMLElement;
  private tracks: readonly Track[] = [];
  private current = 0;
  private playing = false;

  constructor(doc: Document, private readonly deps: SubsSheetDeps) {
    this.doc = doc;
    this.list = h(doc, "div", { id: "subsList" });
    this.note = h(doc, "div", { id: "subsNote" });
    this.root = h(
      doc,
      "div",
      {
        id: "subsSheet",
        class: "sheet",
        role: "dialog",
        modal: true,
        aria: "Subtitles",
      },
      [grip(doc), head(doc, "Subtitles"), this.list, this.note],
    );
  }

  /** Fold a status poll in. Cheap and side-effect-free unless the sheet is
   * open: the manager repaints it when it opens, and main.ts when a poll lands
   * while it already is. */
  onStatus(s: Status): void {
    this.tracks = s.subs || [];
    this.current = s.sub || 0;
    this.playing = s.state === "playing" || s.state === "paused";
  }

  paint(): void {
    this.list.textContent = "";
    setText(this.note, "");

    if (!this.playing) {
      // Reachable in principle -- a film can end while the sheet is open.
      setText(this.note, "Start a film and its subtitles will appear here.");
      return;
    }

    // Off comes first and is always there, because turning them off is the one
    // thing somebody wants in a hurry, in the dark, mid-scene.
    this.list.appendChild(
      row(this.doc, "Off", "", this.current === 0, () => this.deps.choose(0)),
    );
    for (const track of this.tracks) {
      this.list.appendChild(
        row(
          this.doc,
          langName(track.lang, track.title),
          "",
          track.n === this.current,
          () => this.deps.choose(track.n),
        ),
      );
    }
    if (!this.tracks.length) {
      setText(
        this.note,
        "This film has no subtitles the player can show. Run playstick-prep.py " +
          "over the library to extract them.",
      );
    }
  }
}
