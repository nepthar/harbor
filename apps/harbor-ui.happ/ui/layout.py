"""The shell around every page: CSS, JS, nav, and shared HTML fragments."""

import html
import json

NAV = (
  ("/", "Dashboard"),
  ("/snapshots", "Snapshots"),
  ("/apps", "Apps"),
  ("/volumes", "Volumes"),
  ("/catalog", "Catalog"),
  ("/logs", "Activity"),
)

STYLE = """
:root {
  --void: #05070a; --bg: #0c1520; --panel: #121c2a; --border: #1e2c3c;
  --fg: #d8dee6; --muted: #8b95a1; --accent: #3a6a94; --accent-fg: #e8eef4;
  --ok: #5ecf8a; --warn: #fdd305; --off: #4a5562; --bad: #c72057;
  --rosewood: #c72057; --coral: #fc795f; --gold: #fdd305;
}
html { color-scheme: dark; height: 100%; background: var(--void); }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 15px; display: flex; height: 100%; overflow: hidden;
  background: var(--void); color: var(--fg);
  font: 15px/1.5 "Source Sans 3", "Source Sans Pro", sans-serif;
}
/* From mid-left, 45° up and to the right. The 15px gutter is what you
   actually see of it -- it cuts off the top-left of the panel. */
body::before {
  content: ""; position: fixed; left: 0; top: 50%; width: 200vw;
  height: 5.7375rem; pointer-events: none;
  transform-origin: 0 50%;
  transform: rotate(-45deg) translateY(-50%);
  background: linear-gradient(to bottom,
    var(--rosewood) 0 calc(33.33% - 4px),
    transparent calc(33.33% - 4px) calc(33.33% + 4px),
    var(--coral) calc(33.33% + 4px) calc(66.67% - 4px),
    transparent calc(66.67% - 4px) calc(66.67% + 4px),
    var(--gold) calc(66.67% + 4px) 100%);
}
.app {
  position: relative; z-index: 1; flex: 1; display: flex;
  min-width: 0; min-height: 0; background: var(--bg); overflow: hidden;
  border-radius: 12px;
}
nav {
  width: 165px; flex: 0 0 165px; background: var(--bg);
  border-right: 1px solid var(--border); padding: 20px 12px;
  overflow: auto; display: flex; flex-direction: column;
}
.brand {
  font-weight: 600; font-size: 16px; padding: 0 10px 18px; letter-spacing: -0.01em;
}
.brand .ver { display: block; font-weight: 400; font-size: 12px; color: var(--muted); }
.brand .mark, nav a .mark { display: none; }
nav a {
  display: block; padding: 7px 10px; margin-bottom: 2px; border-radius: 6px;
  color: var(--muted); text-decoration: none;
}
nav a:hover {
  background: color-mix(in srgb, var(--accent) 22%, transparent); color: var(--fg);
}
nav a.active { background: var(--accent); color: var(--accent-fg); }
.nav-toggle {
  background: none; color: var(--muted); border: 0; border-radius: 6px;
  padding: 7px; margin-top: auto; cursor: pointer; font: inherit; width: 100%;
}
.nav-toggle:hover {
  color: var(--fg);
  background: color-mix(in srgb, var(--accent) 22%, transparent);
}
html.nav-collapsed nav { width: 48px; flex-basis: 48px; padding: 16px 6px; }
html.nav-collapsed .brand { padding: 0 0 18px; text-align: center; }
html.nav-collapsed .brand .name, html.nav-collapsed .brand .ver,
html.nav-collapsed nav a .label { display: none; }
html.nav-collapsed .brand .mark { display: block; }
html.nav-collapsed nav a { text-align: center; padding: 7px 0; }
html.nav-collapsed nav a .mark { display: inline; }
main { flex: 1; padding: 28px 32px; min-width: 0; overflow: auto; }
.head { display: flex; align-items: center; gap: 14px; margin-bottom: 20px; }
.head .head-actions { margin-left: auto; }
.head .head-actions a.btn {
  color: var(--fg); font-size: 13px; text-decoration: none;
  border: 1px solid var(--border); border-radius: 6px; padding: 5px 12px;
  background: var(--panel);
}
.head .head-actions a.btn:hover {
  border-color: var(--accent); text-decoration: none;
}
.fetchbar {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); padding: 12px 14px; margin-bottom: 16px;
}
.fetchbar input[name=target] { flex: 1 1 28rem; min-width: 16rem; }
.fetchbar .hint {
  flex-basis: 100%; color: var(--muted); font-size: 12px; margin: 0;
}
h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
.head a { color: var(--muted); text-decoration: none; font-size: 13px; }
.head a:hover { color: var(--fg); text-decoration: underline; }
.card {
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  background: var(--panel);
}
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted); font-weight: 500;
  padding: 6px 12px; border-bottom: 1px solid var(--border); white-space: nowrap;
  vertical-align: middle;
}
td {
  padding: 5px 12px; border-bottom: 1px solid var(--border); white-space: nowrap;
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
td.name { font-weight: 500; }
/* Trailing action: hug the right edge. Other columns share the leftover width. */
th.act, td.act { width: 1%; text-align: right; }
.sub { display: block; font-size: 12px; color: var(--muted); font-weight: 400; }
.pill {
  display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
  color: var(--muted);
}
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--off); }
.dot.running { background: var(--ok); }
.dot.exited { background: var(--warn); }
.dot.bad { background: var(--bad); }
.muted { color: var(--muted); }
/* Paths are long and matter; let them wrap rather than push the row's
   controls off the edge. */
td.path { white-space: normal; word-break: break-all; min-width: 16ch; }
td.wrap { white-space: normal; min-width: 20ch; }
h2 { font-size: 15px; margin: 26px 0 6px; font-weight: 600; }
h2:first-child { margin-top: 0; }
h2 .act { font-weight: 400; font-size: 13px; margin-left: 10px; }
h2 .act a { color: var(--muted); text-decoration: none; }
h2 .act a:hover { color: var(--fg); text-decoration: underline; }
.lede { color: var(--muted); margin: 0 0 10px; max-width: 62ch; }
.card.pad { padding: 14px; }
.card.entry { padding: 5px 12px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
/* `display: flex` outranks the UA stylesheet's [hidden] { display: none }. */
.row[hidden] { display: none; }
.row .grow { flex: 1; min-width: 200px; }
input[type=text], input[type=password], input:not([type]) {
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 9px; font: inherit;
}
label { color: var(--muted); display: inline-flex; gap: 5px; align-items: center; }
button {
  background: var(--accent); color: var(--accent-fg); border: 0;
  border-radius: 6px; padding: 3px 10px; font: inherit; line-height: 1.3;
  cursor: pointer;
}
button.link {
  background: none; color: var(--muted); padding: 0; text-decoration: underline;
}
button.link:hover { color: var(--bad); }
.apphead { margin-bottom: 6px; }
.apphead h2 { margin: 0; font-size: 17px; }
.apphead .lede { margin: 6px 0 2px; }
.row.between { justify-content: space-between; width: 100%; }
.actions form { display: inline; }
.actions a.link, .fetchbar a.link {
  color: var(--muted); font-size: 13px; text-decoration: none;
}
.actions a.link:hover, .fetchbar a.link:hover { color: var(--fg); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--muted); margin: 14px 0 6px; font-weight: 500; }
table.kv td { vertical-align: middle; }
table.kv td.key { font-weight: 500; white-space: nowrap; width: 1%; }
table.kv td.key .sub { font-weight: 400; }
table.kv td.field { width: 1%; white-space: nowrap; }
.cfg-edit { display: flex; align-items: center; gap: 8px; }
.cfg-edit input { width: 16em; max-width: 100%; }
.cfg-save { visibility: hidden; padding: 3px 10px; }
.cfg-edit.is-dirty .cfg-save { visibility: visible; }
select {
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 9px; font: inherit;
}
button[disabled] { opacity: .4; cursor: not-allowed; }
button.icon {
  background: var(--accent); color: var(--accent-fg); padding: 4px 8px;
  line-height: 0;
}
button.icon:hover:not(:disabled) {
  color: var(--accent-fg);
  background: color-mix(in srgb, var(--accent) 82%, white);
}
button.icon svg { width: 22px; height: 22px; fill: currentColor; display: block; }
.head-actions .actions { gap: 6px; }
pre {
  margin: 8px 0 0; padding: 10px; background: var(--bg); border-radius: 6px;
  overflow-x: auto; font-size: 12px; white-space: pre-wrap;
}
.error ul { margin: 6px 0 0; padding-left: 18px; }
.error li { margin-bottom: 4px; }
td.name a { color: inherit; text-decoration: none; }
td.name a:hover { text-decoration: underline; }
td.path a { color: inherit; }
td.path a:hover { color: var(--fg); }
.notice {
  border: 1px solid var(--ok); border-radius: 8px; padding: 10px 14px;
  margin-bottom: 16px; background: var(--panel);
}
.empty, .note { padding: 28px; color: var(--muted); text-align: center; }
.error {
  border: 1px solid var(--bad); border-radius: 8px; padding: 16px 18px;
  background: var(--panel);
}
.error h2 { margin: 0 0 6px; font-size: 15px; color: var(--bad); }
.error p { margin: 0 0 4px; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  background: var(--border); padding: 1px 5px; border-radius: 4px;
}
details.reveal > summary {
  cursor: pointer; color: var(--muted); font-size: 13px; padding: 10px 14px;
}
details.reveal > summary:hover { color: var(--fg); }
.card > details.reveal { border-top: 1px solid var(--border); }
.card.pad > details.reveal {
  border-top: none; margin-top: 10px;
}
.card.pad > details.reveal > summary { padding: 6px 0; }
.catalog-row { cursor: pointer; }
.catalog-row:hover td {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.shade {
  position: fixed; inset: 0; z-index: 20;
  display: flex; align-items: center; justify-content: center;
  padding: 24px 32px;
  background: color-mix(in srgb, var(--void) 62%, transparent);
}
.shade[hidden] { display: none; }
.job-modal {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; width: min(36rem, 100%);
  padding: 16px 18px; display: flex; flex-direction: column; gap: 10px;
  max-height: min(80vh, 100%);
}
.job-modal h2 { margin: 0; font-size: 17px; }
.job-modal p { margin: 0; }
.job-bar { width: 100%; }
.job-bar input::placeholder { color: var(--muted); }
.job-fields { width: 100%; gap: 8px; }
.job-out {
  margin: 0; min-height: 12rem; max-height: 40vh; overflow: auto;
  padding: 10px; background: var(--bg); border-radius: 6px;
  font-size: 12px; white-space: pre-wrap;
  border: 1px solid transparent; transition: border-color 400ms ease;
}
.job-choices { display: flex; flex-direction: column; gap: 8px; }
.job-choices[hidden] { display: none; }
.job-choice {
  display: flex; gap: 9px; align-items: flex-start; color: var(--fg);
  border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
  cursor: pointer;
}
.job-choice:has(input:checked) { border-color: var(--accent); }
.job-choice .sub { margin-top: 2px; }
.job-out.ok { border-color: var(--ok); }
.job-out.bad { border-color: var(--bad); }
.app-card {
  display: flex; flex-direction: row; align-items: stretch;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; width: min(88rem, 100%);
  max-height: min(92vh, 100%); overflow: hidden;
  padding: 14px; gap: 12px;
}
.app-card[hidden] { display: none; }
.app-card-head {
  flex: 0 0 20rem; width: 20rem;
  display: flex; flex-direction: column;
  border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px; background: var(--bg);
  overflow: auto;
}
.app-card h2 { margin: 0 0 2px; font-size: 17px; }
.app-card .lede { margin: 4px 0 8px; }
.app-card-intro > :last-child { margin-bottom: 0; }
/* margin-top:auto rather than a fixed footer: the head scrolls, and buttons
   that scrolled away with it would be unreachable on a long description. */
.app-card-head > .actions {
  margin-top: auto; padding-top: 12px;
  border-top: 1px solid var(--border);
}
.app-card .conflict {
  margin: 8px 0 0; padding: 8px 10px; font-size: 13px;
  color: var(--fg); background: var(--panel);
  border-left: 2px solid var(--bad); border-radius: 4px;
}
.app-card .stale {
  margin: 8px 0 0; padding: 8px 10px; font-size: 13px;
  color: var(--fg); background: var(--panel);
  border-left: 2px solid var(--warn); border-radius: 4px;
}
.app-card .update {
  margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--border); font-size: 13px;
}
.app-card .update p { margin: 0 0 4px; }
.app-card .update p:last-child { margin-bottom: 0; }
.app-card-diff span { display: block; }
.app-card-diff .diff-add { color: var(--ok); }
.app-card-diff .diff-del { color: var(--coral); }
.app-card-diff .diff-hunk { color: var(--muted); }
.app-card-manifest {
  flex: 1 1 auto; min-width: 0; min-height: 16rem; overflow: auto;
  margin: 0; padding: 14px 16px;
  background: var(--bg); border-radius: 8px;
  font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre; color: var(--fg);
}
.app-card-manifest code {
  background: none; padding: 0; font: inherit; color: inherit;
}
.hljs-comment { color: var(--muted); }
.hljs-section { color: var(--coral); }
.hljs-attr { color: var(--fg); }
.hljs-string { color: var(--gold); }
.hljs-number, .hljs-literal { color: var(--ok); }
.hljs-punctuation { color: var(--muted); }
.charts { display: flex; gap: 16px; align-items: stretch; }
.charts > .card { flex: 1; min-width: 0; }
.charts h2 { margin: 0 0 8px; }
.chart { height: 220px; }
.chart .muted { margin: 0; padding: 48px 12px 0; text-align: center; }
.uplot { font-family: inherit; }
@media (max-width: 52rem) {
  .app-card { flex-direction: column; }
  .app-card-head { flex: 0 0 auto; width: auto; }
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


def head_actions(path):
  """Page-level actions that belong beside the title rather than in the body."""
  if path != "/catalog":
    return ""
  return (
    '<span class="head-actions">'
    '<a class="btn" href="/catalog?fetch=1">Fetch App</a></span>'
  )


def page(path, title, body, version="", actions=""):
  active = nav_active(path)
  links = "".join(
    f'<a href="{href}" title="{esc(label)}"'
    f"{' class="active"' if href == active else ''}>"
    f'<span class="label">{esc(label)}</span>'
    f'<span class="mark" aria-hidden="true">{esc(label[0])}</span></a>'
    for href, label in NAV
  )
  sub = f'<span class="ver">harbor {esc(version)}</span>' if version else ""
  extra = actions or head_actions(path)
  refresh = (
    ""
    if path.startswith("/apps/") and path != "/apps"
    else f'<a href="{esc(path)}">Refresh</a>'
  )
  return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · harbor</title>
<script>if (localStorage.getItem("harbor-nav") === "collapsed") document.documentElement.classList.add("nav-collapsed");</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600&display=swap" rel="stylesheet">
<style>{STYLE}</style></head>
<body>
<div class="app">
<nav>
  <div class="brand"><span class="name">Harbor</span><span class="mark" aria-hidden="true">H</span>{sub}</div>
  {links}
  <button type="button" class="nav-toggle" aria-label="Collapse sidebar">‹</button>
</nav>
<main>
  <div class="head"><h1>{esc(title)}</h1>{refresh}{extra}</div>
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


def icon_button(label, icon, *, submit=False):
  kind = "submit" if submit else "button"
  return (
    f'<button type="{kind}" class="icon" title="{esc(label)}" '
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
