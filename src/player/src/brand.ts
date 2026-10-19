// The wordmark, as markup. Inlined rather than an <img> for the reason
// template.html gives: the page carries everything it draws, because the stick
// may have no route anywhere and neither may the phone.
//
// Lifted from logo.svg at the repo root -- the same strokes and gradient, with
// the mark and the background left out, since the page is already the dark the
// logo was drawn on. The ids are prefixed because they share a document with
// everything else on the page.
export const WORDMARK = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="320 630 904 212" role="img" aria-label="Playstick"><defs><linearGradient id="psWord" gradientUnits="userSpaceOnUse" x1="766" y1="0" x2="1213" y2="0"><stop offset="0" stop-color="#3a8dfd"/><stop offset="0.2" stop-color="#5f63fa"/><stop offset="0.45" stop-color="#b048e6"/><stop offset="0.65" stop-color="#f0469c"/><stop offset="0.82" stop-color="#fd5d63"/><stop offset="1" stop-color="#fbac52"/></linearGradient><clipPath id="psKArms"><rect x="1130" y="688" width="90" height="95"/></clipPath></defs><g fill="none" stroke-width="27" stroke-linejoin="round"><g stroke="#f3f3f6"><path d="M337.5,825 V701.5 H436.5 V769.5 H363"/><path d="M477.5,648 V783"/><path d="M619.5,783 V701.5 H520.5 V769.5 H594"/><path d="M658.5,688 V769.5 H739.5 M739.5,688 V811.5 H658"/></g><g stroke="url(#psWord)"><path d="M864,700 H780 V735 H853 V771 H767"/><path d="M892,667 V769.5 H948 M879,700.5 H943"/><path d="M973.5,688 V783"/><path d="M1094,701.5 H1014 V769.5 H1093"/><path d="M1119.5,648 V783"/><path d="M1210,676 L1150,735 L1210,794" clip-path="url(#psKArms)" stroke-linejoin="miter"/></g><rect x="959.5" y="648.5" width="28" height="27" rx="6" fill="url(#psWord)" stroke="none"/></g></svg>`;
