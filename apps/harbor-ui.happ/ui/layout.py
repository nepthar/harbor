"""The shell around every page: CSS, JS, nav, and shared HTML fragments."""

import html
from urllib.parse import quote

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
.head { display: flex; align-items: baseline; gap: 14px; margin-bottom: 20px; }
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
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
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
.apphead {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 20px; flex-wrap: wrap; margin-bottom: 6px;
}
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
.cmd-modal {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; width: min(36rem, 100%);
  padding: 16px 18px; display: flex; flex-direction: column; gap: 10px;
  max-height: min(80vh, 100%);
}
.cmd-modal h2 { margin: 0; font-size: 17px; }
.cmd-modal #cmd-desc { margin: 0; }
.cmd-bar { width: 100%; }
.cmd-bar #cmd-command { flex: 0 0 auto; white-space: nowrap; }
.cmd-bar #cmd-args::placeholder { color: var(--muted); }
.cmd-out {
  margin: 0; min-height: 12rem; max-height: 40vh; overflow: auto;
  padding: 10px; background: var(--bg); border-radius: 6px;
  font-size: 12px; white-space: pre-wrap;
}
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
@media (max-width: 52rem) {
  .app-card { flex-direction: column; }
  .app-card-head { flex: 0 0 auto; width: auto; }
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
  """Page-level actions that belong beside the title rather than in the body.

  Only the catalog has one. It is a link, not a form: revealing the fetch bar
  is a change of view, so it survives a refresh and can be linked to.
  """
  if path != "/catalog":
    return ""
  return (
    '<span class="head-actions">'
    '<a class="btn" href="/catalog?fetch=1">Fetch App</a></span>'
  )


def page(path, title, body, version=""):
  active = nav_active(path)
  links = "".join(
    f'<a href="{href}" title="{esc(label)}"'
    f"{' class="active"' if href == active else ''}>"
    f'<span class="label">{esc(label)}</span>'
    f'<span class="mark" aria-hidden="true">{esc(label[0])}</span></a>'
    for href, label in NAV
  )
  sub = f'<span class="ver">harbor {esc(version)}</span>' if version else ""
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
  <div class="head"><h1>{esc(title)}</h1><a href="{esc(path)}">Refresh</a>{head_actions(path)}</div>
  {body}
</main>
</div>
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


def error_card(message):
  return (
    f'<div class="error"><h2>Cannot reach harbord</h2>'
    f"<p>{esc(message)}</p>"
    f'<p class="muted">The socket is bound with '
    f"<code>harbor config harbor-ui --bind conn=&lt;host_volume&gt;</code>.</p></div>"
  )


def job_card(job):
  if not job:
    return ""
  state = job["state"]
  if state in ("queued", "running"):
    return (
      f'<div class="notice"><b>{esc(job["verb"])}</b> is {esc(state)}. '
      f"Refresh to see how it ended.</div>"
    )
  if state == "failed":
    return (
      f'<div class="error"><h2>{esc(job["verb"])} failed</h2>'
      f"<pre>{esc(job['error'])}</pre></div>"
    )
  body = ""
  if job.get("log"):
    dirname, _, filename = job["log"].partition("/")
    body = (
      f' <a href="/logs?app={quote(dirname)}&amp;file={quote(filename)}">'
      f"View its output</a>."
    )
  return f'<div class="notice"><b>{esc(job["verb"])}</b> finished.{body}</div>'


def kv_table(pairs):
  rows = "".join(
    f'<tr><td class="key">{esc(k)}</td><td class="muted path">{esc(v)}</td></tr>'
    for k, v in pairs
  )
  return f'<div class="scroll"><table class="kv"><tbody>{rows}</tbody></table></div>'
