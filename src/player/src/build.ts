// Noticing that the page itself has been replaced.
//
// A deploy rewrites the served page and restarts the daemon, but every phone in
// the house still has the OLD page open and polls it happily forever -- nobody
// refreshes. So the page compares the build it was stamped with against the one
// the daemon reports and reloads when they differ.
//
// The whole risk is WHEN: a reload forgets which film it is following, throws
// away the clock offset the sync spent a minute measuring, and drops the audio
// element out of somebody's ears. So it only fires when there is nothing to
// lose -- which there always is shortly after a deploy, because restarting the
// daemon stops the film.
import type { PlayerState } from "./types";

// Rewritten by the daemon on the way out (see ui_page() in http.py); this
// literal only survives in a copy that reached a browser some other way. It must
// appear exactly once in the bundle -- build.mjs refuses to ship otherwise.
export const BUILD = "__PLAYSTICK_BUILD__";

/**
 * Cache-busting for the two URLs a browser is allowed to KEEP -- a poster (held
 * for up to a year once it is an extracted frame) and a soundtrack (an hour). In
 * the QUERY, never the path: every route matches on the parsed path, so nothing
 * on the daemon reads this. It is a cache key and nothing else.
 */
export function stamped(path: string, build: string = BUILD): string {
  return path + "?v=" + build;
}

export interface ReloadPolicyDeps {
  reload: () => void;
}

export class ReloadPolicy {
  private reloading = false;

  constructor(
    private readonly deps: ReloadPolicyDeps,
    private readonly build: string = BUILD,
  ) {}

  /**
   * Given the build the daemon just reported and what is on screen, reload if
   * and only if it is safe to. Returns true when a reload was asked for.
   */
  consider(statusBuild: string | undefined, state: PlayerState): boolean {
    // Nothing to do on a page the daemon never stamped (an older daemon sends
    // no build, and a page that reloaded on that would reload forever) or on
    // the build it is still serving.
    if (!statusBuild || statusBuild === this.build) {
      return false;
    }
    // Not while there is something to throw away.
    if (state === "playing" || state === "paused" || state === "preparing") {
      return false;
    }
    // reload() asks for a navigation, it does not end this turn: polls already
    // in flight still land and see the same mismatch. Asking twice is at best
    // pointless and at worst a page that keeps restarting its own load.
    if (this.reloading) {
      return false;
    }
    this.reloading = true;
    this.deps.reload();
    return true;
  }
}
