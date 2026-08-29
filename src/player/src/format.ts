// Small pure formatters shared by the views.

/**
 * "1 h 24 min left" / "45 min left". Rounded to the minute on purpose -- there
 * is no seek bar and no second-by-second countdown, because a control that can
 * lose a child's place is a control that produces tears. Never negative.
 */
export function timeLeft(seconds: number): string {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  if (h && m) {
    return `${h} h ${m} min left`;
  }
  if (h) {
    return `${h} h left`;
  }
  return `${m} min left`;
}

/** A percentage width, one decimal, clamped to [0, 100]. */
export function barWidth(position: number, duration: number): string {
  if (!(duration > 0)) {
    return "0";
  }
  const pct = Math.max(0, Math.min(100, (100 * position) / duration));
  return pct.toFixed(1) + "%";
}

/**
 * "1:12:34" / "12:44". Seconds, unlike timeLeft() above, and that is not an
 * inconsistency: this one labels a place somebody is about to jump to, and a
 * jump reported to the nearest minute is one you cannot tell from the one you
 * already made. Never negative.
 */
export function clockTime(seconds: number): string {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
}

/** "+0:10" / "−8:30" -- a jump, with the sign a person reads rather than a
 * minus that could be a hyphen. */
export function jumpLabel(seconds: number): string {
  return (seconds < 0 ? "−" : "+") + clockTime(Math.abs(seconds));
}
