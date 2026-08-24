// The stylesheet, assembled from the pieces each view keeps next to the markup
// it styles, and installed as one <style> before anything is built.
//
// It ships inside the bundle rather than in the template because the markup does
// too: a rule and the element it describes are one decision, and splitting them
// across two files is how a page ends up with styles for elements that no longer
// exist. There is nothing to flash: the body is empty until the inline script
// runs, so the first paint is already styled.
//
// The order below is the order these rules had when they lived in template.html.
// Nothing in here relies on it -- the selectors are disjoint by section -- but
// keeping it means the cascade is the one that was tested on a phone.
import { BANNER_CSS, PREPARING_CSS, PLAYING_CSS } from "./views";
import { LIBRARY_CSS } from "./library-view";
import { SHEET_CSS } from "./sheet";
import { SOUND_CSS } from "./sound-sheet";
import { FILTER_CSS } from "./filter-sheet";

/* Everything here is sized for a child holding a phone in a dark room. The
   poster tiles are the buttons, the play/pause control is 40% of the screen,
   and there is no seek bar -- a control that can lose your place is a control
   that produces tears. */
const BASE = `
:root {
  --bg: #0b0b0d;
  --card: #17171c;
  --ink: #f2f2f5;
  --dim: #9a9aa8;
  --accent: #4da3ff;
  --stop: #d8443c;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%;
  background: var(--bg); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  /* A child taps twice, drags, and long-presses. None of that should zoom the
     page, select text, or flash a grey box. */
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
  -webkit-user-select: none; user-select: none;
  overscroll-behavior: none;
}
button { font: inherit; color: inherit; border: 0; background: none; cursor: pointer; }
button:disabled { opacity: .45; }

.view { display: none; padding: env(safe-area-inset-top) 12px calc(20px + env(safe-area-inset-bottom)); }
.view.on { display: block; }
`;

export const CSS = [
  BASE,
  BANNER_CSS,
  LIBRARY_CSS,
  PREPARING_CSS,
  PLAYING_CSS,
  SHEET_CSS,
  SOUND_CSS,
  FILTER_CSS,
].join("\n");

/** Put the stylesheet in the head. Called first, before any view is built. */
export function installStyles(doc: Document): void {
  const style = doc.createElement("style");
  style.textContent = CSS;
  doc.head.appendChild(style);
}
