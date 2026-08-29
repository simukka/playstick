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
  /** The CC button, also built by SheetManager. Unlike the funnel it is not a
   * property of which view is up -- see below -- so the switcher does not own
   * it. Absent in the tests that do not care about it. */
  subsBtn?: HTMLElement;
  /** Tapped when the banner offers to switch the NAS on. Absent in the tests
   * that do not care, and then the offer is simply never made. */
  wakeNas?: () => void;
}

export class StatusPresenter {
  readonly views: ViewSwitcher;
  readonly banner: Banner;
  readonly preparing: PreparingView;
  readonly playing: PlayingView;
  private readonly grid: HTMLElement;
  private readonly reload: ReloadPolicy;
  private readonly wakeNas?: () => void;
  private readonly subsBtn?: HTMLElement;

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
    this.wakeNas = deps.wakeNas;
    this.subsBtn = deps.subsBtn;
  }

  apply(s: Status): void {
    this.state = s.state;
    this.reload.consider(s.build, s.state);
    this.paintSubsBtn(s);

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
      this.unreachable(s);
    } else {
      this.banner.show("");
    }
  }

  /**
   * The CC button: shown while a film is up and only when there is something to
   * offer, lit while subtitles are actually on.
   *
   * Here rather than in ViewSwitcher, which owns the funnel, because this is
   * not a question about which view is drawn. A film with no text subtitles --
   * an unprepped share, or one whose only subtitles are the image-based kind
   * prep declines to OCR -- must not get a button that opens a sheet saying
   * "nothing". The answer only arrives with the poll.
   */
  private paintSubsBtn(s: Status): void {
    if (!this.subsBtn) {
      return;
    }
    const up = s.state === "playing" || s.state === "paused";
    toggleClass(this.subsBtn, "on", up && !!s.subs?.length);
    toggleClass(this.subsBtn, "live", up && !!s.sub);
  }

  /**
   * The films cannot be reached. Three different sentences, because they ask
   * for three different things from whoever is holding the phone.
   *
   * The old one said "the NAS may be off" and stopped there: accurate, and
   * useless, because it named the fix and offered no way to apply it. Somebody
   * had to walk to the NAS and press its button. When the daemon can tell that
   * the machine really is off AND knows how to wake it, the same sentence gets
   * the button under it.
   *
   * The fallback is the important branch rather than the tidy one: a daemon
   * from before this existed sends no `nas` field at all, and a NAS whose MAC
   * was never configured reports can_wake false. Both must land on the message
   * the page has always shown rather than on a button that does nothing.
   */
  private unreachable(s: Status): void {
    if (s.nas?.power === "waking") {
      // No button: it has just been pressed, and offering it again would invite
      // a second tap the daemon is only going to refuse.
      this.banner.show("Waking the NAS up \u2014 this takes a minute.");
      return;
    }
    if (s.nas?.power === "off" && s.nas.can_wake && this.wakeNas) {
      this.banner.show(
        "Can't reach the movies \u2014 the NAS is off.",
        0,
        { label: "Turn it on", onTap: this.wakeNas },
      );
      return;
    }
    this.banner.show("Can't reach the movies right now \u2014 the NAS may be off.");
  }
}
