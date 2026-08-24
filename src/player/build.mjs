// Bundles the TypeScript UI into one self-contained page.
//
// The device is offline and has no Node: it is handed a single static HTML file
// by Ansible (and by the dev GUI container). So the build inlines the whole
// bundle into template.html at the //__BUNDLE__ marker and writes the result to
// dist/playstick-ui.html, which is committed. Nothing external is fetched at
// runtime -- no module graph, no CDN, no separate ui.js.
//
// Not minified on purpose. The page carries a ?debug overlay and the journal
// telemetry is meant to be read against source; a readable bundle is worth more
// here than the few kB minifying would save on a LAN.
//
// renderPage() is exported because tests/page.test.ts boots the page in a real
// DOM: what it boots has to be the page this writes, not a re-creation of it.
import { build } from "esbuild";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const TOKEN = "__PLAYSTICK_BUILD__";
const MARKER = "//__BUNDLE__";

/** The whole page, as a string: the bundle inlined into the template. */
export async function renderPage() {
  const result = await build({
    entryPoints: [join(here, "src/main.ts")],
    bundle: true,
    format: "iife",
    target: "es2018",
    charset: "utf8",
    legalComments: "none",
    write: false,
  });

  const js = result.outputFiles[0].text.trimEnd();

  const tokens = (js.match(new RegExp(TOKEN, "g")) || []).length;
  if (tokens !== 1) {
    throw new Error(
      `bundle must carry the build token exactly once, found ${tokens}. ` +
        `The daemon rewrites it (see http.py); zero means no cache-busting and ` +
        `two means a half-rewritten page.`,
    );
  }

  const template = readFileSync(join(here, "template.html"), "utf8");
  if (!template.includes(MARKER)) {
    throw new Error(`template.html is missing the ${MARKER} marker`);
  }
  return { page: template.replace(MARKER, () => js), js };
}

/** Write it where Ansible and the gui container look for it. */
export async function writePage() {
  const { page, js } = await renderPage();
  mkdirSync(join(here, "dist"), { recursive: true });
  writeFileSync(join(here, "dist/playstick-ui.html"), page);
  return { page, js };
}

// Only when run as the build, not when a test imports it.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { page, js } = await writePage();
  console.log(
    `dist/playstick-ui.html written: ${page.length} bytes ` +
      `(${js.length} of them script)`,
  );
}
