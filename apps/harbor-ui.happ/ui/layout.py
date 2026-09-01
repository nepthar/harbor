"""The shell around every page: CSS, JS, nav, and shared HTML fragments."""

import html
import json

NAV = (
  ("/", "Dashboard"),
  ("/snapshots", "Snapshots"),
  ("/apps", "Apps"),
  ("/volumes", "Volumes"),
  ("/catalog", "Repos"),
  ("/logs", "Activity"),
)

STYLE = """
/* ---------------------------------------------------------------------------
   Tokens. Two surfaces, three text weights, one radius, one spacing scale.
   The ground is neutral-warm rather than navy so the signal colours (which are
   all warm) sit on it instead of fighting a blue cast.
   --------------------------------------------------------------------------- */
:root {
  --void: #100f0e;    /* the gutter the ribbon runs through */
  --bg: #171614;      /* the app ground */
  --panel: #1f1c19;   /* raised: wells, inputs, modals */
  --line: #2e2925;    /* hairline that separates */
  --hair: #232020;    /* hairline that is barely there (table rows) */

  /* Three text weights, each with exactly one job. Never pure white. */
  --fg: #ece5dc;      /* content */
  --dim: #bfb5a8;     /* labels, secondary */
  --muted: #9a9082;   /* metadata, disabled */

  /* The ribbon, reused as meaning. Nothing in the UI is coloured off-palette. */
  --rosewood: #c72057;
  --coral: #fc795f;
  --gold: #fdd305;
  --ok: #8aa85e;

  --accent: var(--coral);
  --ink: #17110e;     /* dark type on a warm fill */
  --bad: var(--rosewood);
  --warn: var(--gold);
  --off: #4a433d;

  --r: 2px;           /* one radius, near-square */
  --gutter: 20px;     /* what you see of the ribbon */
  --chamfer: 54px;    /* the cut corner the ribbon runs through */
  --ribbon: 30px;
  --ribbon-top: 45px;
}
html { color-scheme: dark; height: 100%; background: var(--void); }
* { box-sizing: border-box; }
body {
  margin: 0; padding: var(--gutter); display: flex; height: 100%; overflow: hidden;
  background: var(--void); color: var(--fg);
  font: 14px/1.55 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
/* The ribbon cuts the top-left corner at 45°, entering the left gutter and
   leaving through the top one. Its centreline runs (0, C) -> (C, 0) where
   C = ribbon-top + ribbon/2; the panel's chamfer is cut wide enough to clear
   it, so the whole run reads as one gesture instead of two stray slivers. */
body::before {
  content: ""; position: fixed; left: 0; top: var(--ribbon-top);
  width: 200vw; height: var(--ribbon); pointer-events: none;
  transform-origin: 0 50%;
  transform: rotate(-45deg) translateY(-50%);
  /* Three bands of 26.67% and two gaps of 10%. Splitting on thirds and then
     insetting the gaps takes two edges off the middle band and one off each
     of its neighbours, which leaves it visibly thinner. */
  background: linear-gradient(to bottom,
    var(--rosewood) 0 26.67%,
    transparent 26.67% 36.67%,
    var(--coral) 36.67% 63.33%,
    transparent 63.33% 73.33%,
    var(--gold) 73.33% 100%);
}
.app {
  position: relative; z-index: 1; flex: 1; display: flex;
  min-width: 0; min-height: 0; background: var(--bg); overflow: hidden;
  clip-path: polygon(0 var(--chamfer), var(--chamfer) 0,
                     100% 0, 100% 100%, 0 100%);
}

/* --- nav ---------------------------------------------------------------- */
nav {
  width: 168px; flex: 0 0 168px; background: var(--bg);
  border-right: 1px solid var(--line);
  padding: calc(var(--chamfer) + 4px) 16px 16px;
  overflow: auto; display: flex; flex-direction: column;
}
.brand { padding: 0 0 24px; letter-spacing: -0.005em; }
.brand .name { font-size: 17px; font-weight: 600; display: block; }
.brand .ver {
  display: block; font-size: 11px; color: var(--muted);
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  margin-top: 1px;
}
/* The ribbon again, flat: the mark and the corner are the same object. */
.brand .mark, nav a .mark { display: none; }
/* Active is a coral edge and coral type -- not a filled pill. The 2px inset
   border keeps every item on the same baseline grid whether lit or not. */
nav a {
  display: block; padding: 5px 10px; margin-bottom: 1px;
  border-left: 2px solid transparent;
  color: var(--dim); text-decoration: none; text-transform: lowercase;
  font-size: 14px;
}
nav a:hover { color: var(--fg); border-left-color: var(--line); }
nav a.active { color: var(--coral); border-left-color: var(--coral); font-weight: 500; }
.nav-toggle {
  background: none; color: var(--muted); border: 0; border-radius: var(--r);
  padding: 6px; margin-top: auto; cursor: pointer; font: inherit; width: 100%;
}
.nav-toggle:hover { color: var(--fg); background: var(--panel); }
html.nav-collapsed nav { width: 52px; flex-basis: 52px; padding: calc(var(--chamfer) + 4px) 8px 16px; }
html.nav-collapsed .brand { padding: 0 0 24px; }
html.nav-collapsed .brand .name, html.nav-collapsed .brand .ver,
html.nav-collapsed nav a .label { display: none; }
html.nav-collapsed .brand .mark { display: block; font-weight: 600; font-size: 17px; }
html.nav-collapsed nav a { padding: 5px 0 5px 8px; }
html.nav-collapsed nav a .mark { display: inline; }

/* --- page head ---------------------------------------------------------- */
main { flex: 1; padding: 28px 32px 48px; min-width: 0; overflow: auto; }
.head {
  position: relative;
  display: flex; align-items: baseline; gap: 16px; margin-bottom: 24px;
  padding-bottom: 12px; border-bottom: 1px solid var(--line);
}
/* The ribbon, flattened onto the head rule: the first 44px of the line is the
   mark. `bottom: -2px` centres 3px on the 1px border, and absolute positioning
   keeps it out of both the flex flow and the page's vertical rhythm. */
.head::after {
  content: ""; position: absolute; left: 0; bottom: -2px;
  width: 44px; height: 3px;
  transform: skewX(-45deg); transform-origin: bottom left;
  background: linear-gradient(to right,
    var(--rosewood) 0 30%, transparent 30% 35%,
    var(--coral) 35% 65%, transparent 65% 70%, var(--gold) 70% 100%);
}
h1 { font-size: 19px; margin: 0; font-weight: 600; letter-spacing: -0.012em; }
/* Beside the title on the head's baseline: what the page is, in one phrase. */
.head-sub { color: var(--dim); font-size: 13px; margin: 0; }
.head a { color: var(--dim); text-decoration: none; font-size: 12px; }
.head a:hover { color: var(--coral); }
.head .head-actions { order: 1; align-self: center; }
.head > a { order: 2; margin-left: auto; }

/* --- section labels ------------------------------------------------------
   A small lowercase label with a rule running to the right edge. Hierarchy
   comes from this and from space, so most content needs no box at all. */
h2 {
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: lowercase; color: var(--dim);
  margin: 32px 0 12px; display: flex; align-items: center; gap: 12px;
}
h2::after { content: ""; order: 1; flex: 1; height: 1px; background: var(--line); }
h2:first-child { margin-top: 0; }
/* Beside the label, before the rule -- the same reading path as `.head`. */
h2 .act {
  order: 0; display: flex; gap: 8px; font-weight: 400; font-size: 12px;
  letter-spacing: 0; text-transform: none;
}
h2 .act button { padding: 2px 9px; font-size: 12px; }
h2 .act a { color: var(--muted); text-decoration: none; }
h2 .act a:hover { color: var(--coral); }
h3 {
  font-size: 11px; letter-spacing: .08em; text-transform: lowercase;
  color: var(--muted); margin: 20px 0 8px; font-weight: 600;
}
.lede { color: var(--dim); margin: -4px 0 12px; max-width: 68ch; font-size: 13px; }
/* A repo's provenance line: long enough to want the full width. */
.lede.repo-meta { max-width: none; color: var(--muted); font-size: 12px; }
.lede.repo-meta .mono { font-size: 12px; }
.warnish { color: var(--warn); }

/* --- surfaces ------------------------------------------------------------
   `.card` around a table is just a pair of rules; only `.pad` fills. */
.card { border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.card.pad {
  border: 1px solid var(--line); border-radius: var(--r);
  background: var(--panel); padding: 16px;
}
.card.entry { padding: 6px 0; border-bottom: none; }
.scroll { overflow-x: auto; }

/* --- tables --------------------------------------------------------------
   Headers are small lowercase mono, not uppercase tracked grey. Identifiers,
   versions, sizes and counts are mono so columns actually line up. */
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; font-size: 11px; font-weight: 400; color: var(--muted);
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  text-transform: lowercase; letter-spacing: 0; font-size: 11.5px;
  padding: 8px 14px 8px 0; border-bottom: 1px solid var(--line);
  white-space: nowrap; vertical-align: middle;
}
td {
  padding: 9px 14px 9px 0; border-bottom: 1px solid var(--hair);
  white-space: nowrap; vertical-align: middle;
}
th:first-child, td:first-child { padding-left: 2px; }
tr:last-child td { border-bottom: none; }
td.name { font-weight: 500; color: var(--fg); }
th.act, td.act { width: 1%; text-align: right; padding-right: 2px; }
.sub {
  display: block; font-size: 11.5px; color: var(--muted); font-weight: 400;
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
tbody tr:hover td { background: color-mix(in srgb, var(--coral) 5%, transparent); }
/* Paths are long and matter; let them wrap rather than push the row's
   controls off the edge. */
td.path { white-space: normal; word-break: break-all; min-width: 16ch; }
td.wrap { white-space: normal; min-width: 20ch; }
td.name a { color: inherit; text-decoration: none; }
td.name a:hover { color: var(--coral); }
td.path a { color: inherit; }
td.path a:hover { color: var(--coral); }

/* Secondary table columns are data, not chrome: readable, and mono so that
   versions, sizes, ids and paths line up down the column. `.wrap` is prose
   (descriptions, hints) and stays in the text face. */
td.muted { color: var(--dim); }
td.muted:not(.wrap), td.path {
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; font-size: 12.5px;
}
td.muted.wrap { color: var(--muted); font-size: 13px; }

/* --- status --------------------------------------------------------------
   Square markers, matching the geometry everywhere else. */
.pill { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--dim); }
.dot { width: 6px; height: 6px; border-radius: 1px; background: var(--off); flex: none; }
.dot.running { background: var(--ok); }
.dot.exited { background: var(--warn); }
.dot.bad { background: var(--bad); }
.muted { color: var(--muted); }
.mono { font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; font-size: 12px; }

/* --- controls ------------------------------------------------------------
   Default is quiet: a hairline and dim type. Fill is reserved for the one
   action on a page that commits something, and it is warm with dark ink --
   not white-on-blue. */
input[type=text], input[type=password], input:not([type]), select {
  background: var(--void); color: var(--fg); border: 1px solid var(--line);
  border-radius: var(--r); padding: 6px 9px; font: inherit; font-size: 13px;
}
input:focus, select:focus, button:focus-visible, a:focus-visible {
  outline: 2px solid var(--coral); outline-offset: 1px;
}
input::placeholder { color: var(--muted); }
label { color: var(--dim); display: inline-flex; gap: 6px; align-items: center; font-size: 13px; }
button {
  background: transparent; color: var(--dim);
  border: 1px solid var(--line); border-radius: var(--r);
  padding: 4px 11px; font: inherit; font-size: 13px; line-height: 1.5;
  cursor: pointer; transition: color 120ms, border-color 120ms;
}
button:hover:not(:disabled) { color: var(--fg); border-color: var(--dim); }
button[disabled] { opacity: .35; cursor: not-allowed; }
button.link {
  background: none; border: 0; color: var(--muted); padding: 0;
  text-decoration: underline; text-underline-offset: 2px;
}
button.link:hover:not(:disabled) { color: var(--bad); border: 0; }
/* The committing action -- one filled control per surface, warm with dark
   ink. Everything else on the page stays a hairline. */
.cfg-save, #job-go, #ask-go,
.app-card-head > .actions > .job-open:first-child {
  background: var(--coral); border-color: var(--coral); color: var(--ink);
  font-weight: 600;
}
.cfg-save:hover:not(:disabled),
#job-go:hover:not(:disabled), #ask-go:hover:not(:disabled),
.app-card-head > .actions > .job-open:first-child:hover:not(:disabled) {
  background: color-mix(in srgb, var(--coral) 86%, white);
  border-color: color-mix(in srgb, var(--coral) 86%, white); color: var(--ink);
}
.cfg-save { visibility: hidden; }
.cfg-edit.is-dirty .cfg-save { visibility: visible; }
.cfg-edit { display: flex; align-items: center; gap: 10px; }
.cfg-edit input { width: 16em; max-width: 100%; }
/* Icon buttons are ghosts that take on their verb's colour on hover. */
button.icon { padding: 5px 7px; line-height: 0; color: var(--dim); }
button.icon svg { width: 20px; height: 20px; fill: currentColor; display: block; }
button.icon:hover:not(:disabled) { color: var(--fg); border-color: var(--dim); }
button.danger:hover:not(:disabled) { color: var(--bad); border-color: var(--bad); }
.head-actions .actions { gap: 8px; }
.head .head-actions a.btn {
  color: var(--fg); font-size: 13px; text-decoration: none;
  border: 1px solid var(--line); border-radius: var(--r); padding: 4px 11px;
  background: transparent;
}
.head .head-actions a.btn:hover { color: var(--coral); border-color: var(--coral); }
.actions form { display: inline; }
.actions a.link, .fetchbar a.link {
  color: var(--muted); font-size: 13px; text-decoration: none;
}
.actions a.link:hover, .fetchbar a.link:hover { color: var(--coral); }

/* --- rows and wells ------------------------------------------------------ */
.row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
/* `display: flex` outranks the UA stylesheet's [hidden] { display: none }. */
.row[hidden] { display: none; }
.row .grow { flex: 1; min-width: 200px; }
.row.between { justify-content: space-between; width: 100%; }
.fetchbar {
  display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  border: 1px solid var(--line); border-radius: var(--r);
  background: var(--panel); padding: 14px; margin-bottom: 20px;
}
.fetchbar input[name=target] { flex: 1 1 28rem; min-width: 16rem; }
.fetchbar .hint { flex-basis: 100%; color: var(--muted); font-size: 12px; margin: 0; }
.apphead { margin-bottom: 10px; }
.apphead h2 {
  margin: 0; font-size: 19px; font-weight: 600; text-transform: none;
  letter-spacing: -0.012em; color: var(--fg); display: block;
}
.apphead h2::after { display: none; }
.apphead .lede { margin: 8px 0 2px; }
table.kv td { vertical-align: middle; }
table.kv td.key { font-weight: 500; white-space: nowrap; width: 1%; padding-right: 28px; }
table.kv td.key .sub { font-weight: 400; }
table.kv td.field { width: 1%; white-space: nowrap; }
pre {
  margin: 10px 0 0; padding: 12px 14px; background: var(--void);
  border: 1px solid var(--line); border-radius: var(--r);
  overflow-x: auto; font-size: 12px; white-space: pre-wrap;
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
code {
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; font-size: 12px;
  background: var(--panel); border: 1px solid var(--line);
  padding: 0 5px; border-radius: var(--r); color: var(--dim);
}
.empty, .note { padding: 48px 0; color: var(--muted); text-align: center; font-size: 13px; }

/* --- notices -------------------------------------------------------------
   A colour-bearing left rule rather than a coloured box. */
.notice.contested ul { margin: 6px 0 0; padding-left: 18px; }
.notice.contested li { margin-bottom: 3px; font-size: 13px; color: var(--dim); }
.notice, .error {
  border: 1px solid var(--line); border-left: 2px solid var(--ok);
  border-radius: var(--r); padding: 12px 16px; margin-bottom: 20px;
  background: var(--panel);
}
.error { border-left-color: var(--bad); }
.notice.contested { border-left-color: var(--warn); }
.error h2 {
  margin: 0 0 8px; font-size: 13px; color: var(--bad); display: block;
  text-transform: none; letter-spacing: 0;
}
.error h2::after { display: none; }
.error p { margin: 0 0 4px; font-size: 13px; }
.error ul { margin: 6px 0 0; padding-left: 18px; }
.error li { margin-bottom: 4px; }

/* --- disclosure ---------------------------------------------------------- */
details.reveal > summary {
  cursor: pointer; color: var(--muted); font-size: 12px; padding: 10px 0;
  list-style-position: outside;
}
details.reveal > summary:hover { color: var(--coral); }
.card > details.reveal { border-top: 1px solid var(--hair); }
.card.pad > details.reveal { border-top: none; margin-top: 12px; }

/* --- catalog / overlays --------------------------------------------------- */
.catalog-row { cursor: pointer; }
.catalog-row:hover td { background: color-mix(in srgb, var(--coral) 8%, transparent); }
.shade {
  position: fixed; inset: 0; z-index: 20;
  /* A contested id opens one card per repo, side by side. */
  display: flex; align-items: center; justify-content: center; gap: 20px;
  padding: 32px;
  background: color-mix(in srgb, var(--void) 72%, transparent);
}
.shade[hidden] { display: none; }
.job-modal {
  background: var(--bg); border: 1px solid var(--line);
  border-radius: var(--r); width: min(36rem, 100%);
  padding: 20px; display: flex; flex-direction: column; gap: 12px;
  max-height: min(80vh, 100%);
}
.job-modal h2 {
  margin: 0; font-size: 16px; font-weight: 600; display: block;
  text-transform: none; letter-spacing: -0.01em; color: var(--fg);
}
.job-modal h2::after { display: none; }
.job-modal p { margin: 0; font-size: 13px; color: var(--dim); }
.job-bar { width: 100%; }
.job-fields { width: 100%; gap: 10px; }
.job-out {
  margin: 0; min-height: 12rem; max-height: 40vh; overflow: auto;
  padding: 12px; background: var(--void); border-radius: var(--r);
  font-size: 12px; white-space: pre-wrap;
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  border: 1px solid var(--line); border-left: 2px solid var(--line);
  transition: border-color 400ms ease;
}
.job-out.ok { border-left-color: var(--ok); }
.job-out.bad { border-left-color: var(--bad); }
.job-choices { display: flex; flex-direction: column; gap: 8px; }
.job-choices[hidden] { display: none; }
.job-choice {
  display: flex; gap: 10px; align-items: flex-start; color: var(--fg);
  border: 1px solid var(--line); border-radius: var(--r); padding: 10px 12px;
  cursor: pointer; font-size: 13px;
}
.job-choice:hover { border-color: var(--dim); }
.job-choice:has(input:checked) { border-color: var(--coral); }
.job-choice .sub { margin-top: 2px; }

/* --- app card ------------------------------------------------------------- */
.app-card {
  display: flex; flex-direction: row; align-items: stretch;
  background: var(--bg); border: 1px solid var(--line);
  border-radius: var(--r); width: min(88rem, 100%);
  max-height: min(92vh, 100%); overflow: hidden;
  padding: 16px; gap: 16px;
}
.app-card[hidden] { display: none; }
.app-card-head {
  flex: 0 0 21rem; width: 21rem;
  display: flex; flex-direction: column;
  border-right: 1px solid var(--line); padding-right: 16px;
  overflow: auto;
}
.app-card h2 {
  margin: 0 0 2px; font-size: 17px; font-weight: 600; display: block;
  text-transform: none; letter-spacing: -0.01em; color: var(--fg);
}
.app-card h2::after { display: none; }
.app-card .lede { margin: 8px 0 12px; }
.app-card-intro > :last-child { margin-bottom: 0; }
/* margin-top:auto rather than a fixed footer: the head scrolls, and buttons
   that scrolled away with it would be unreachable on a long description. */
.app-card-head > .actions {
  margin-top: auto; padding-top: 14px; border-top: 1px solid var(--line);
}
.app-card .conflict, .app-card .stale {
  margin: 10px 0 0; padding: 8px 12px; font-size: 12.5px;
  color: var(--dim); background: var(--panel);
  border-left: 2px solid var(--bad); border-radius: var(--r);
}
.app-card .stale { border-left-color: var(--warn); }
/* Free-form docker options the manifest passes through. Louder than `.stale`:
   a tinted ground as well as a rule, because this is the one thing on the card
   the operator is being asked to agree to. */
.app-card .passthru {
  margin: 12px 0 0; padding: 10px 12px; font-size: 12.5px;
  border-left: 2px solid var(--bad); border-radius: var(--r);
  background: color-mix(in srgb, var(--bad) 10%, var(--panel));
  color: var(--dim);
}
.app-card .passthru b {
  display: block; margin-bottom: 6px; font-weight: 600; color: var(--bad);
}
.app-card .passthru p { margin: 0 0 8px; }
.app-card .passthru ul { margin: 0; padding-left: 16px; }
.app-card .passthru li { margin: 3px 0; }
.app-card .passthru li .mono { color: var(--fg); font-size: 12px; }
.app-card .update {
  margin-top: 12px; padding-top: 12px;
  border-top: 1px solid var(--line); font-size: 12.5px;
}
.app-card .update p { margin: 0 0 4px; }
.app-card .update p:last-child { margin-bottom: 0; }
.app-card-diff span { display: block; }
.app-card-diff .diff-add { color: var(--ok); }
.app-card-diff .diff-del { color: var(--coral); }
.app-card-diff .diff-hunk { color: var(--muted); }
.app-card-manifest {
  flex: 1 1 auto; min-width: 0; min-height: 16rem; overflow: auto;
  margin: 0; padding: 4px 0 0;
  background: none; border: 0; border-radius: 0;
  font: 12.5px/1.6 "IBM Plex Mono", ui-monospace, Menlo, monospace;
  white-space: pre; color: var(--dim);
}
.app-card-manifest code {
  background: none; border: 0; padding: 0; font: inherit; color: inherit;
}
.hljs-comment { color: var(--muted); font-style: italic; }
.hljs-section { color: var(--coral); font-weight: 500; }
.hljs-attr { color: var(--fg); }
.hljs-string { color: var(--gold); }
.hljs-number, .hljs-literal { color: var(--ok); }
.hljs-punctuation { color: var(--muted); }

/* --- charts --------------------------------------------------------------- */
.charts { display: flex; gap: 20px; align-items: stretch; }
.charts > .card { flex: 1; min-width: 0; }
.charts h2 { margin: 0 0 10px; display: block; }
.charts h2::after { display: none; }
.chart { height: 220px; }
.chart .muted { margin: 0; padding: 48px 12px 0; text-align: center; font-size: 13px; }
.uplot { font-family: inherit; }

@media (max-width: 52rem) {
  .app-card { flex-direction: column; }
  .app-card-head {
    flex: 0 0 auto; width: auto;
    border-right: 0; padding-right: 0;
    border-bottom: 1px solid var(--line); padding-bottom: 14px;
  }
  .charts { flex-direction: column; }
}
"""


def fmt_size(n):
  if n is None:
    return "&mdash;"
  size = float(n)
  for unit in ("B", "KB", "MB", "GB", "TB"):
    if size < 1024:
      return f"{size:.1f} {unit}"
    size /= 1024
  return f"{size:.1f} PB"


def esc(value):
  return html.escape("" if value is None else str(value))


def nav_active(path):
  """Which nav entry a path belongs to. `/apps/<id>` keeps Apps lit, and the
  Apps link still goes back to the list."""
  for href, _ in NAV:
    if path == href or (href != "/" and path.startswith(href + "/")):
      return href
  return None


def page(path, title, body, version="", actions="", subtitle=""):
  active = nav_active(path)
  links = "".join(
    f'<a href="{href}" title="{esc(label)}"'
    f"{' class="active"' if href == active else ''}>"
    f'<span class="label">{esc(label)}</span>'
    f'<span class="mark" aria-hidden="true">{esc(label[0])}</span></a>'
    for href, label in NAV
  )
  sub = f'<span class="ver">harbor {esc(version)}</span>' if version else ""
  extra = actions
  refresh = (
    ""
    if path.startswith("/apps/") and path != "/apps"
    else f'<a href="{esc(path)}">Refresh</a>'
  )
  lede = f'<p class="head-sub">{esc(subtitle)}</p>' if subtitle else ""
  return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · harbor</title>
<script>if (localStorage.getItem("harbor-nav") === "collapsed") document.documentElement.classList.add("nav-collapsed");</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{STYLE}</style></head>
<body>
<div class="app">
<nav>
  <div class="brand"><span class="name">Harbor</span><span class="mark" aria-hidden="true">H</span>{sub}</div>
  {links}
  <button type="button" class="nav-toggle" aria-label="Collapse sidebar">‹</button>
</nav>
<main>
  <div class="head"><h1>{esc(title)}</h1>{lede}{refresh}{extra}</div>
  {body}
</main>
</div>
{confirm_modal()}
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/languages/toml.min.js"></script>
<script>
(function () {{
  var root = document.documentElement;
  var btn = document.querySelector(".nav-toggle");
  function sync() {{
    var on = root.classList.contains("nav-collapsed");
    btn.setAttribute("aria-label", on ? "Expand sidebar" : "Collapse sidebar");
    btn.textContent = on ? "›" : "‹";
  }}
  sync();
  btn.addEventListener("click", function () {{
    var on = root.classList.toggle("nav-collapsed");
    if (on) localStorage.setItem("harbor-nav", "collapsed");
    else localStorage.removeItem("harbor-nav");
    sync();
  }});
  document.querySelectorAll(".cfg-edit").forEach(function (form) {{
    var input = form.querySelector("input:not([type=hidden])");
    var save = form.querySelector(".cfg-save");
    if (!input || !save) return;
    function dirty() {{
      var on = input.value !== input.defaultValue;
      form.classList.toggle("is-dirty", on);
      save.disabled = !on;
    }}
    input.addEventListener("input", dirty);
  }});
  var fetchShade = document.getElementById("fetch-shade");
  if (fetchShade) {{
    fetchShade.addEventListener("click", function (event) {{
      if (event.target === fetchShade) {{
        window.location = fetchShade.getAttribute("data-close");
      }}
    }});
  }}
  var shade = document.getElementById("catalog-shade");
  if (shade) {{
    function closeCard() {{
      var closeTo = shade.getAttribute("data-close");
      if (closeTo) {{ window.location = closeTo; return; }}
      shade.hidden = true;
      shade.querySelectorAll(".app-card").forEach(function (card) {{
        card.hidden = true;
      }});
    }}
    document.querySelectorAll(".catalog-row").forEach(function (row) {{
      row.addEventListener("click", function () {{
        var card = document.getElementById(row.getAttribute("data-card"));
        if (!card) return;
        shade.querySelectorAll(".app-card").forEach(function (other) {{
          other.hidden = true;
        }});
        card.hidden = false;
        shade.hidden = false;
      }});
    }});
    shade.addEventListener("click", function (event) {{
      if (event.target === shade) closeCard();
    }});
  }}
  // Timestamps ship as UTC in `datetime`; only the browser knows the viewer's
  // zone, so the friendly text is filled in here. Absolute local time stays on
  // the tooltip, and the ISO fallback survives with no JS.
  function relTime(then, now) {{
    var secs = Math.round((now - then) / 1000);
    if (secs < 0) return "just now";
    if (secs < 45) return "just now";
    var mins = Math.round(secs / 60);
    if (mins < 60) return mins + "m ago";
    var hours = Math.round(mins / 60);
    if (hours < 24) return hours + "h ago";
    var days = Math.round(hours / 24);
    if (days < 30) return days + "d ago";
    return then.toLocaleDateString(undefined,
      {{ year: "numeric", month: "short", day: "numeric" }});
  }}
  document.querySelectorAll("time[datetime]").forEach(function (el) {{
    var then = new Date(el.getAttribute("datetime"));
    if (isNaN(then.getTime())) return;
    el.textContent = relTime(then, new Date());
    el.title = then.toLocaleString();
  }});
  if (window.hljs) {{
    document.querySelectorAll("pre.app-card-manifest code").forEach(function (el) {{
      hljs.highlightElement(el);
    }});
  }}
}})();
</script>
</body></html>"""


MDI = {
  "play": "M8,5.14V19.14L19,12.14L8,5.14Z",
  "stop": "M18,18H6V6H18V18Z",
  "refresh": (
    "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 "
    "18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 "
    "0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z"
  ),
  "database-export": (
    "M12,3C7.58,3 4,4.79 4,7C4,9.21 7.58,11 12,11C12.5,11 13,10.97 13.5,10.92V9.5"
    "H16.39L15.39,8.5L18.9,5C17.5,3.8 14.94,3 12,3M18.92,7.08L17.5,8.5L20,11H15V13"
    "H20L17.5,15.5L18.92,16.92L23.84,12M4,9V12C4,14.21 7.58,16 12,16C13.17,16 "
    "14.26,15.85 15.25,15.63L16.38,14.5H13.5V12.92C13,12.97 12.5,13 12,13C7.58,13 "
    "4,11.21 4,9M4,14V17C4,19.21 7.58,21 12,21C14.94,21 17.5,20.2 18.9,19L17,17.1"
    "C15.61,17.66 13.9,18 12,18C7.58,18 4,16.21 4,14Z"
  ),
  "database-import": (
    "M12,3C8.59,3 5.69,4.07 4.54,5.57L9.79,10.82C10.5,10.93 11.22,11 12,11C16.42,"
    "11 20,9.21 20,7C20,4.79 16.42,3 12,3M3.92,7.08L2.5,8.5L5,11H0V13H5L2.5,15.5"
    "L3.92,16.92L8.84,12M20,9C20,11.21 16.42,13 12,13C11.34,13 10.7,12.95 10.09,"
    "12.87L7.62,15.34C8.88,15.75 10.38,16 12,16C16.42,16 20,14.21 20,12M20,14C20,"
    "16.21 16.42,18 12,18C9.72,18 7.67,17.5 6.21,16.75L4.53,18.43C5.68,19.93 8.59,"
    "21 12,21C16.42,21 20,19.21 20,17"
  ),
  "trash-can": (
    "M9,3V4H4V6H5V19A2,2 0 0,0 7,21H17A2,2 0 0,0 19,19V6H20V4H15V3H9M9,8H11V17H9"
    "V8M13,8H15V17H13V8Z"
  ),
  "delete-outline": (
    "M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19M8,9H16V19H8V9M15.5,4L14.5,3"
    "H9.5L8.5,4H5V6H19V4H15.5Z"
  ),
  "plus-box-outline": (
    "M19,19V5H5V19H19M19,3A2,2 0 0,1 21,5V19A2,2 0 0,1 19,21H5A2,2 0 0,1 3,19V5"
    "C3,3.89 3.9,3 5,3H19M11,7H13V11H17V13H13V17H11V13H7V11H11V7Z"
  ),
}


def mdi(name):
  path = MDI.get(name)
  if path is None:
    raise ValueError(f"unknown icon {name!r}")
  return (
    f'<svg class="mdi" viewBox="0 0 24 24" aria-hidden="true"><path d="{path}"/></svg>'
  )


def icon_button(label, icon, *, submit=False, danger=False):
  kind = "submit" if submit else "button"
  klass = "icon danger" if danger else "icon"
  return (
    f'<button type="{kind}" class="{klass}" title="{esc(label)}" '
    f'aria-label="{esc(label)}">{mdi(icon)}</button>'
  )


def job_button(
  label,
  verb="",
  *,
  title,
  desc="",
  args=None,
  fields=(),
  choices=(),
  enabled=True,
  autorun=False,
  done="",
  icon="",
  danger=False,
):
  """A button that opens the job modal. See `job_modal` for the attributes.

  `choices` offers several verbs behind one button, each with its own
  wording; the operator picks before Run is live. `icon` is an MDI name;
  `label` is then the hover tooltip rather than the button text.
  """
  extra = "" if enabled else " disabled"
  landing = f' data-done="{esc(done)}"' if done else ""
  if icon:
    klass = "job-open icon"
    tip = f' title="{esc(label)}" aria-label="{esc(label)}"'
    content = mdi(icon)
  else:
    klass = "job-open"
    tip = ""
    content = esc(label)
  if danger:
    klass += " danger"
  return (
    f'<button type="button" class="{klass}"{extra}{tip}'
    f' data-verb="{esc(verb)}" data-title="{esc(title)}" data-desc="{esc(desc)}"'
    f" data-args='{esc(json.dumps(args or {}))}'"
    f" data-fields='{esc(json.dumps(list(fields)))}'"
    f" data-choices='{esc(json.dumps(list(choices)))}'"
    f"{landing}{' data-autorun=1' if autorun else ''}>{content}</button>"
  )


def log_button(label, log, status, *, title, klass="link"):
  """A button that opens a finished run in the job modal, read-only."""
  return (
    f'<button type="button" class="job-open {esc(klass)}"'
    f' data-title="{esc(title)}" data-log="{esc(log)}"'
    f' data-status="{esc(status)}">{esc(label)}</button>'
  )


def job_modal():
  """The dialog every job verb runs through: describe, Run, tail the log.

  One per page. Buttons opt in with class `job-open` and carry the verb, the
  wording, fixed args, and any fields the operator fills in.
  """
  return (
    '<div id="job-shade" class="shade" hidden>'
    '<div class="job-modal" role="dialog" aria-modal="true" aria-labelledby="job-title">'
    '<div class="row between">'
    '<h2 id="job-title"></h2>'
    '<button type="button" id="job-dismiss" hidden>Close</button>'
    "</div>"
    '<p class="muted" id="job-desc"></p>'
    '<div class="job-choices" id="job-choices" hidden></div>'
    '<div class="row job-fields" id="job-fields"></div>'
    '<div class="row job-bar" id="job-bar">'
    '<button type="button" id="job-go">ok</button>'
    '<button type="button" id="job-close">cancel</button>'
    "</div>"
    '<pre id="job-out" class="job-out" hidden></pre>'
    "</div></div>" + _JOB_SCRIPT
  )


_JOB_SCRIPT = """
<script>
(function () {
  var shade = document.getElementById("job-shade");
  if (!shade) return;
  var titleEl = document.getElementById("job-title");
  var descEl = document.getElementById("job-desc");
  var fieldsEl = document.getElementById("job-fields");
  var choicesEl = document.getElementById("job-choices");
  var outEl = document.getElementById("job-out");
  var bar = document.getElementById("job-bar");
  var go = document.getElementById("job-go");
  var close = document.getElementById("job-close");
  var dismiss = document.getElementById("job-dismiss");
  var timer = null, jobId = null, verb = null, fixed = {}, ran = false, done = null;
  var choices = [];

  function stopPoll() { if (timer) { clearInterval(timer); timer = null; } }

  function hide() {
    stopPoll();
    shade.hidden = true;
    // The page behind is stale once a job has run: its status, config and
    // last-action all moved. Come back with the job id so the page can say
    // how it ended.
    if (ran) {
      // Status, config and last-action all moved; the page behind is stale.
      window.location.href = done || window.location.href;
    }
  }

  function showText(text) {
    outEl.hidden = false;
    outEl.textContent = text || "";
    outEl.scrollTop = outEl.scrollHeight;
  }

  function pullLog(log) {
    if (!log) return Promise.resolve();
    return fetch("/activity/" + encodeURIComponent(log)).then(function (r) {
      if (!r.ok) return;
      return r.json().then(function (body) {
        if (body && body.text != null) showText(body.text);
      });
    }).catch(function () {});
  }

  function poll() {
    if (!jobId) return;
    fetch("/jobs/" + encodeURIComponent(jobId)).then(function (r) {
      return r.json();
    }).then(function (job) {
      var done = job.state === "done" || job.state === "failed";
      return pullLog(job.log).then(function () {
        if (!done) return;
        stopPoll();
        if (!job.log && job.error) showText(job.error);
        outEl.classList.add(job.state === "done" ? "ok" : "bad");
      });
    }).catch(function () {});
  }

  function chosen() {
    if (!choices.length) return { verb: verb, args: fixed };
    var picked = choicesEl.querySelector("input[name=job-choice]:checked");
    return choices[picked ? Number(picked.value) : 0];
  }

  function submit() {
    var pick = chosen();
    var args = {};
    Object.keys(pick.args || {}).forEach(function (k) { args[k] = pick.args[k]; });
    fieldsEl.querySelectorAll("input").forEach(function (input) {
      if (input.value.trim()) args[input.name] = input.value;
    });
    ran = true;
    fieldsEl.querySelectorAll("input").forEach(function (i) { i.disabled = true; });
    choicesEl.querySelectorAll("input").forEach(function (i) { i.disabled = true; });
    // Nothing left to cancel, so the bar goes and Close moves up beside the
    // title, clear of the output.
    bar.hidden = true;
    dismiss.hidden = false;
    showText("queued\u2026");
    fetch("/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verb: pick.verb, args: args })
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (!res.ok) { showText(res.body.error || "failed"); return; }
      jobId = res.body.id;
      timer = setInterval(poll, 1000);
      poll();
    }).catch(function (err) { showText(String(err)); });
  }

  document.querySelectorAll(".job-open").forEach(function (btn) {
    btn.addEventListener("click", function () {
      stopPoll();
      jobId = null; ran = false;
      var log = btn.getAttribute("data-log");
      verb = btn.getAttribute("data-verb");
      fixed = JSON.parse(btn.getAttribute("data-args") || "{}");
      done = btn.getAttribute("data-done");
      titleEl.textContent = btn.getAttribute("data-title") || verb;
      descEl.textContent = btn.getAttribute("data-desc") || "";
      descEl.hidden = !descEl.textContent;
      fieldsEl.innerHTML = "";
      (JSON.parse(btn.getAttribute("data-fields") || "[]")).forEach(function (f) {
        var input = document.createElement("input");
        input.name = f.name;
        input.placeholder = f.placeholder || f.name;
        input.className = "grow";
        input.autocomplete = "off";
        fieldsEl.appendChild(input);
      });
      fieldsEl.hidden = fieldsEl.children.length === 0;
      choices = JSON.parse(btn.getAttribute("data-choices") || "[]");
      choicesEl.innerHTML = "";
      choices.forEach(function (c, i) {
        var label = document.createElement("label");
        label.className = "job-choice";
        var radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "job-choice";
        radio.value = String(i);
        if (i === 0) radio.checked = true;
        var text = document.createElement("span");
        text.innerHTML = "";
        var strong = document.createElement("b");
        strong.textContent = c.label;
        var note = document.createElement("span");
        note.className = "sub";
        note.textContent = c.desc || "";
        text.appendChild(strong);
        text.appendChild(note);
        label.appendChild(radio);
        label.appendChild(text);
        choicesEl.appendChild(label);
      });
      choicesEl.hidden = choices.length === 0;
      outEl.textContent = "";
      outEl.classList.remove("ok", "bad");
      outEl.hidden = true;
      bar.hidden = false;
      go.hidden = false;
      go.disabled = false;
      dismiss.hidden = true;
      shade.hidden = false;
      if (log) {
        bar.hidden = true;
        dismiss.hidden = false;
        outEl.classList.add(btn.getAttribute("data-status") === "ok" ? "ok" : "bad");
        pullLog(log);
        return;
      }
      if (btn.getAttribute("data-autorun")) {
        submit();
      } else if (fieldsEl.children.length) {
        fieldsEl.querySelector("input").focus();
      } else {
        go.focus();
      }
    });
  });

  go.addEventListener("click", submit);
  close.addEventListener("click", hide);
  dismiss.addEventListener("click", hide);
  shade.addEventListener("click", function (event) {
    if (event.target === shade) hide();
  });
})();
</script>
"""


def confirm_modal():
  """The gate on any form carrying `data-confirm`. One per page."""
  return (
    '<div id="ask-shade" class="shade" hidden>'
    '<div class="job-modal" role="dialog" aria-modal="true" aria-labelledby="ask-title">'
    '<h2 id="ask-title">Are you sure?</h2>'
    '<p class="muted" id="ask-text"></p>'
    '<div class="row job-bar">'
    '<button type="button" id="ask-go">Yes, continue</button>'
    '<button type="button" id="ask-close">Cancel</button>'
    "</div></div></div>" + _ASK_SCRIPT
  )


_ASK_SCRIPT = """
<script>
(function () {
  var shade = document.getElementById("ask-shade");
  if (!shade) return;
  var textEl = document.getElementById("ask-text");
  var go = document.getElementById("ask-go");
  var close = document.getElementById("ask-close");
  var pending = null;

  function hide() { shade.hidden = true; pending = null; }

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.confirmed) return;
      event.preventDefault();
      pending = form;
      textEl.textContent = form.getAttribute("data-confirm");
      shade.hidden = false;
      go.focus();
    });
  });

  go.addEventListener("click", function () {
    if (!pending) return;
    pending.dataset.confirmed = "1";
    pending.submit();
    hide();
  });
  close.addEventListener("click", hide);
  shade.addEventListener("click", function (e) { if (e.target === shade) hide(); });
})();
</script>
"""


def error_card(message):
  return (
    f'<div class="error"><h2>Cannot reach harbord</h2>'
    f"<p>{esc(message)}</p>"
    f'<p class="muted">The socket is bound with '
    f"<code>harbor config harbor-ui --bind conn=&lt;host_volume&gt;</code>.</p></div>"
  )


def kv_table(pairs):
  rows = "".join(
    f'<tr><td class="key">{esc(k)}</td><td class="muted path">{esc(v)}</td></tr>'
    for k, v in pairs
  )
  return f'<div class="scroll"><table class="kv"><tbody>{rows}</tbody></table></div>'
