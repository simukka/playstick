// Turning one status poll into what is on the screen.
//
// This is the old apply(): a single function whose whole job is to be the one
// place that decides which view is up and what the banner says. It reads the
// state the daemon reports, never the DOM, and it hands the details off to the
// views. The reload check runs first, before any branch, because whether the
// page may quietly replace itself is a question about what is playing, not about
// which view happens to be drawn.
import { toggleClass } from "./dom";
import type { ReloadPolicy } from "./build";
import { Banner, PlayingView, PreparingView, ViewSwitcher } from "./views";
import type { PlayerState, Status } from "./types";

export interface PresenterDeps {
  reload: ReloadPolicy;
  stamp: (path: string) => string;
  now?: () => number;
  /** The grid view and its tile parent, built by LibraryView: the presenter
   * switches to the view and blocks the grid during AirPlay, but owns neither. */
  library: HTMLElement;
  grid: HTMLElement;
  /** The funnel, built by SheetManager: only the grid may show it. */
  filterBtn: HTMLElement;
}

export class StatusPresenter {
  readonly views: ViewSwitcher;
  readonly banner: Banner;
  readonly preparing: PreparingView;
  readonly playing: PlayingView;
  private readonly grid: HTMLElement;
  private readonly reload: ReloadPolicy;

  /** The state the last poll reported, read by the controls to decide what the
   * play/pause button does. */
  state: PlayerState = "idle";

  constructor(doc: Document, deps: PresenterDeps) {
    this.banner = new Banner(doc, deps.now);
    this.preparing = new PreparingView(doc, deps.stamp);
    this.playing = new PlayingView(doc);
    this.views = new ViewSwitcher({
      library: deps.library,
      preparing: this.preparing.root,
      playing: this.playing.root,
      filterBtn: deps.filterBtn,
    });
    this.grid = deps.grid;
    this.reload = deps.reload;
  }

  apply(s: Status): void {
    this.state = s.state;
    this.reload.consider(s.build, s.state);

    if (s.state === "preparing") {
      this.views.show("preparing");
      this.preparing.apply(s);
      return;
    }

    if (s.state === "playing" || s.state === "paused") {
      this.views.show("playing");
      // Said once the picture is up, not during the wait: while the steps run
      // the daemon is still retrying, and a warning about something it may yet
      // recover from is noise. If the picture never appears, this explains why.
      this.banner.show(
        s.projector?.fault
          ? "The movie is playing, but I couldn't reach the projector."
          : "",
      );
      this.playing.apply(s);
      return;
    }

    // Anything else means the projector is ours to offer again.
    this.views.show("library");
    toggleClass(this.grid, "blocked", s.state === "airplay");
    if (s.notice) {
      // A preparation that gave up on a background thread: this poll is the only
      // way the reason reaches the phone.
      this.banner.show(s.notice);
    } else if (s.state === "airplay") {
      this.banner.show(
        "Someone is using the projector to show their phone. " +
          "The movies come back when they stop.",
      );
    } else if (s.state === "unavailable") {
      this.banner.show("Can't reach the movies right now \u2014 the NAS may be off.");
    } else {
      this.banner.show("");
    }
  }
}
