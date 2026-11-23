// The projector's own settings, behind the gear beside "Turn projector on/off"
// in the logo's menu. For grown-ups, like the rest of that menu.
//
// WHAT IT LISTS IS WHAT THE DRIVER SAYS. Every group and every choice comes
// from GET /api/projector/settings, which the driver builds from its table of
// what the projector answers over the cable. Nothing here names a picture mode
// or an input code, so a second projector's settings appear without a change
// to this file.
//
// IT ASKS WHEN IT OPENS, NOT ON EVERY POLL. The values are serial exchanges,
// not part of /api/status, so they are fetched on opening and re-read from the
// answer to each change -- which is what the projector says after the change,
// not what was asked for. A value it echoed and then ignored shows as the old
// one, with the daemon's sentence underneath.
//
// ONE CHANGE AT A TIME. A tap while one is in flight is ignored: each change
// is a command and a read-back on a single serial line, and a second finger
// queued behind the first would only make the first answer arrive later.
import { h, head, grip, setText } from "./dom";
import { row } from "./sheet";
import type { PostResult } from "./net";
import type { ProjectorSetting, ProjectorSettings } from "./types";

export const PROJECTOR_CSS = `
.projGroup { margin-bottom: 4px; }
#projNote {
  color: var(--dim); font-size: 16px; line-height: 1.45; padding: 10px 14px 2px;
  min-height: 1.45em;
}
#projBack { margin-top: 18px; }
`;

export const ASKING = "Asking the projector…";
export const OFF_TEXT = "The projector is off. Turn it on to change its settings.";

export interface ProjectorSheetDeps {
  load: () => Promise<PostResult>;
  change: (name: string, value: string) => Promise<PostResult>;
  /** Back to the logo's menu. */
  back: () => void;
}

export class ProjectorSheet {
  readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly list: HTMLElement;
  private readonly note: HTMLElement;
  private settings: ProjectorSetting[] = [];
  private busy = false;
  /** Bumped on every open, so an answer to an earlier open is dropped. */
  private generation = 0;

  constructor(doc: Document, private readonly deps: ProjectorSheetDeps) {
    this.doc = doc;
    this.list = h(doc, "div", { id: "projList" });
    this.note = h(doc, "div", { id: "projNote" });
    const backRow = row(doc, "Back", "", false, () => this.deps.back());
    backRow.classList.add("act");
    const back = h(doc, "div", { id: "projBack" }, [backRow]);
    this.root = h(
      doc,
      "div",
      { id: "projectorSheet", class: "sheet", role: "dialog", modal: true, aria: "Projector" },
      [grip(doc), head(doc, "Projector"), this.note, this.list, back],
    );
  }

  /** Called when the sheet opens: ask the projector afresh. */
  opened(): void {
    const mine = ++this.generation;
    this.busy = false;
    this.settings = [];
    this.paint();
    setText(this.note, ASKING);
    void this.deps.load().then(
      (r) => {
        if (mine === this.generation) {
          this.apply(r, "");
        }
      },
      () => {
        if (mine === this.generation) {
          setText(this.note, "The stick did not answer.");
        }
      },
    );
  }

  /** The answer to a load or a change: new settings, or the reason why not. */
  private apply(r: PostResult, done: string): void {
    if (!r.ok) {
      setText(this.note, r.data.error || "The projector didn't answer.");
      return;
    }
    const answer = r.data as unknown as ProjectorSettings;
    this.settings = answer.settings ?? [];
    this.paint();
    setText(this.note, this.settings.length ? done : OFF_TEXT);
  }

  paint(): void {
    this.list.textContent = "";
    for (const s of this.settings) {
      const group = h(this.doc, "div", { class: "projGroup" });
      for (const c of s.choices) {
        group.appendChild(
          row(this.doc, c.label, "", c.value === s.value, () => this.choose(s, c.value)),
        );
      }
      this.list.appendChild(head(this.doc, s.label));
      this.list.appendChild(group);
    }
  }

  private choose(s: ProjectorSetting, value: string): void {
    if (this.busy || value === s.value) {
      return;
    }
    this.busy = true;
    const mine = this.generation;
    setText(this.note, "…");
    void this.deps.change(s.name, value).then(
      (r) => {
        if (mine !== this.generation) {
          return;
        }
        this.busy = false;
        this.apply(r, "");
      },
      () => {
        if (mine !== this.generation) {
          return;
        }
        this.busy = false;
        setText(this.note, "The stick did not answer.");
      },
    );
  }
}
