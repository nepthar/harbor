#!/usr/bin/env python3
"""harbor-ui — a server-rendered view of harbor state.

Every page is one request to harbord over its unix socket, rendered to HTML and
returned. Nothing polls: a page is a photograph, and Refresh takes a new one.
That is deliberate -- the reads behind it walk the filesystem the way
`harbor ps` does, and a browser tab left open should not be a background load
on the box.
"""

import errno
import html
import http.client
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

SOCKET = os.environ.get("HARBOR_SOCKET", "/harbor/conn/admin.sock")
# host:port wins over the socket when set. Docker Desktop's bind mounts cannot
# carry AF_UNIX, so a mac host serves this over TCP instead.
API = os.environ.get("HARBOR_API", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

NAV = (
  ("/", "Dashboard"),
  ("/apps", "Apps"),
  ("/volumes", "Volumes"),
  ("/catalog", "Catalog"),
  ("/logs", "Logs"),
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
/* Mid-left to mid-top: atan2 keeps the band on those two points as the
   viewport changes. The 15px gutter is what you actually see of it. */
body::before {
  content: ""; position: fixed; left: 0; top: 50%; width: 200vw;
  height: 5.7375rem; pointer-events: none;
  transform-origin: 0 50%;
  transform: rotate(atan2(-100vh, 100vw)) translateY(-50%);
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
  padding: 10px 14px; border-bottom: 1px solid var(--border); white-space: nowrap;
}
td { padding: 11px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; }
tr:last-child td { border-bottom: none; }
td.name { font-weight: 500; }
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
  border-radius: 6px; padding: 7px 13px; font: inherit; cursor: pointer;
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
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--muted); margin: 14px 0 6px; font-weight: 500; }
table.kv td { vertical-align: middle; }
table.kv td.key { font-weight: 500; white-space: nowrap; width: 1%; }
table.kv td.key .sub { font-weight: 400; }
table.kv td.field { width: 1%; white-space: nowrap; }
.cfg-edit { display: flex; align-items: center; gap: 8px; }
.cfg-edit input { width: 16em; max-width: 100%; }
.cfg-save { visibility: hidden; padding: 5px 10px; }
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
"""


class UnixHTTPConnection(http.client.HTTPConnection):
  """http.client over AF_UNIX. The Host header is a formality here."""

  def __init__(self, path):
    super().__init__("localhost", timeout=10)
    self._unix_path = path

  def connect(self):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(self.timeout)
    sock.connect(self._unix_path)
    self.sock = sock


class ApiError(Exception):
  """harbord could not be reached, or refused. Rendered, never raised at a user."""


def where():
  """Human-readable name for whichever transport is in play."""
  return API if API else SOCKET


def connect():
  if not API:
    return UnixHTTPConnection(SOCKET)
  address = API.split("://", 1)[-1].rstrip("/")
  host, _, port = address.rpartition(":")
  if not port.isdigit():
    raise ApiError(f"HARBOR_API must be host:port, got {API!r}")
  return http.client.HTTPConnection(host or "127.0.0.1", int(port), timeout=10)


def api(path, method="GET", payload=None):
  conn = connect()
  body = json.dumps(payload).encode() if payload is not None else None
  headers = {"Content-Type": "application/json"} if body else {}
  try:
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    status = response.status
  except FileNotFoundError as e:
    raise ApiError(
      f"No socket at {SOCKET}. Is harbord running, and is $harbor/conn bound "
      f"into this container?"
    ) from e
  except OSError as e:
    hint = ""
    # EOPNOTSUPP on a socket that is plainly there is Docker Desktop: its bind
    # mounts cannot carry AF_UNIX, and no amount of rebinding will fix it.
    if not API and e.errno == errno.EOPNOTSUPP:
      hint = (
        " This host's bind mounts cannot carry a unix socket (Docker Desktop "
        "does not support it). Run `harbord --port N --host 0.0.0.0` and set "
        "`harbor config harbor-ui --set api_address=host.docker.internal:N`."
      )
    raise ApiError(f"Cannot reach harbord at {where()}: {e}.{hint}") from e
  finally:
    conn.close()

  try:
    body = json.loads(raw)
  except ValueError as e:
    raise ApiError(f"harbord sent something that is not JSON ({status})") from e
  if status >= 400:
    raise ApiError(str(body.get("error", body)))
  return body


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


def page(path, title, body, version=""):
  active = nav_active(path)
  links = "".join(
    f'<a href="{href}" title="{esc(label)}"'
    f'{" class=\"active\"" if href == active else ""}>'
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
  <div class="head"><h1>{esc(title)}</h1><a href="{esc(path)}">Refresh</a></div>
  {body}
</main>
</div>
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
}})();
</script>
</body></html>"""


def error_card(message):
  return (
    f'<div class="error"><h2>Cannot reach harbord</h2>'
    f"<p>{esc(message)}</p>"
    f"<p class=\"muted\">The socket is bound with "
    f"<code>harbor config harbor-ui --bind conn=&lt;host_volume&gt;</code>.</p></div>"
  )


def status_cell(app):
  state = app.get("status") or "unknown"
  containers = app.get("containers") or {}
  running, total = containers.get("running", 0), containers.get("total", 0)
  css = state if state in ("running", "exited") else ""
  detail = f"{running}/{total}" if total else "no containers"
  return (
    f'<span class="pill"><span class="dot {css}"></span>{esc(state)}</span>'
    f'<span class="sub">{esc(detail)}</span>'
  )


def config_cell(app):
  configured = app.get("configured")
  if configured is None:
    return '<span class="muted">&mdash;</span>'
  if configured == "ready":
    return '<span class="pill"><span class="dot running"></span>ready</span>'
  return '<span class="pill"><span class="dot bad"></span>needs config</span>'


def apps_table(apps):
  if not apps:
    return (
      '<div class="card"><p class="empty">No apps installed yet. '
      "Fetch one with <code>harbor fetch</code>.</p></div>"
    )
  rows = []
  for app in apps:
    name = app.get("display_name") or app.get("app_id")
    version = app.get("version")
    rows.append(
      "<tr>"
      f'<td class="name">'
      f'<a href="/apps/{quote(str(app.get("app_id")))}">{esc(name)}</a>'
      f'<span class="sub">{esc(app.get("app_id"))}</span></td>'
      f"<td>{status_cell(app)}</td>"
      f"<td>{config_cell(app)}</td>"
      f'<td class="muted">{esc(version or "&mdash;")}</td>'
      f'<td class="muted">{esc(app.get("volume_count", 0))}</td>'
      f'<td class="muted">{esc(app.get("last_action") or "—")}</td>'
      "</tr>"
    )
  return (
    '<div class="card scroll"><table><thead><tr>'
    "<th>App</th><th>Status</th><th>Config</th>"
    "<th>Version</th><th>Volumes</th><th>Last action</th>"
    "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
  )


def host_volume_rows(entries):
  if not entries:
    return '<p class="empty">No host volumes declared yet.</p>'
  rows = []
  for entry in entries:
    missing = (
      "" if entry["exists"] else '<span class="sub">path is missing</span>'
    )
    flags = ", ".join(
      flag
      for flag, on in (("read-only", entry["readonly"]),
                       ("require mount", entry["require_mount"]))
      if on
    )
    rows.append(
      "<tr>"
      f'<td class="name">{esc(entry["tag"])}</td>'
      f'<td class="muted path">{esc(entry["path"])}{missing}</td>'
      f'<td class="muted">{esc(flags or "—")}</td>'
      f'<td><form method="post" action="/volumes">'
      f'<input type="hidden" name="action" value="delete">'
      f'<input type="hidden" name="tag" value="{esc(entry["tag"])}">'
      f'<button class="link" type="submit">Delete</button></form></td>'
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Tag</th><th>Path</th>'
    "<th>Flags</th><th></th></tr></thead><tbody>"
    + "".join(rows)
    + "</tbody></table></div>"
  )


def host_volume_form():
  return (
    '<form method="post" action="/volumes" class="row">'
    '<input type="hidden" name="action" value="create">'
    '<input name="tag" placeholder="tag (e.g. media)" required>'
    '<input name="path" placeholder="/mnt/media" required class="grow">'
    '<label><input type="checkbox" name="readonly"> read-only</label>'
    '<label><input type="checkbox" name="require_mount"> require mount</label>'
    '<button type="submit">Add</button></form>'
  )


def volume_rows(volumes, sizes):
  if not volumes:
    return '<p class="empty">No app volumes on disk yet.</p>'
  rows = []
  for volume in volumes:
    if volume["in_use"]:
      use = '<span class="pill"><span class="dot running"></span>in use</span>'
    else:
      use = '<span class="pill"><span class="dot"></span>idle</span>'
    orphan = (
      "" if volume["declared"] else '<span class="sub">not in the manifest</span>'
    )
    size = fmt_size(volume["bytes"]) if sizes else "&mdash;"
    rows.append(
      "<tr>"
      f'<td class="name">{esc(volume["name"])}{orphan}</td>'
      f'<td class="muted">{esc(volume["app_id"])}</td>'
      f'<td class="muted">{esc(volume["kind"])}</td>'
      f"<td>{use}</td>"
      f'<td class="muted">{size}</td>'
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Volume</th><th>App</th>'
    "<th>Kind</th><th>Use</th><th>Size</th></tr></thead><tbody>"
    + "".join(rows)
    + "</tbody></table></div>"
  )


def volumes_page(sizes, notice=""):
  host = api("/host-volumes")["host_volumes"]
  volumes = api("/volumes?sizes=1" if sizes else "/volumes")["volumes"]
  measure = (
    '<a href="/volumes">Hide sizes</a>'
    if sizes
    else '<a href="/volumes?sizes=1">Measure sizes</a>'
  )
  return (
    notice
    + '<h2>Host volumes</h2>'
    + '<p class="lede">Paths on this machine that apps may bind to. An app '
      "asks for one in its manifest; you decide which directory it gets.</p>"
    + f'<div class="card">{host_volume_rows(host)}</div>'
    + f'<div class="card pad">{host_volume_form()}</div>'
    + f'<h2>App volumes <span class="act">{measure}</span></h2>'
    + '<p class="lede">Harbor-managed storage under your volume roots. '
      "Sizes are measured on request: it walks every file, which is usually "
      "quick and occasionally not.</p>"
    + f'<div class="card">{volume_rows(volumes, sizes)}</div>'
  )


def kv_table(pairs):
  rows = "".join(
    f'<tr><td class="key">{esc(k)}</td><td class="muted path">{esc(v)}</td></tr>'
    for k, v in pairs
  )
  return f'<div class="scroll"><table class="kv"><tbody>{rows}</tbody></table></div>'


def lifecycle_bar(app):
  """Start, stop and stage. Each posts a job and comes back with its id."""
  running = app["status"] == "running"
  buttons = [
    ("start", "Start", not running),
    ("stop", "Stop", running),
    ("stage", "Re-stage", True),
  ]
  return '<div class="row actions">' + "".join(
    f'<form method="post" action="/apps/{quote(app["app_id"])}">'
    f'<input type="hidden" name="action" value="{verb}">'
    f'<button type="submit"{"" if enabled else " disabled"}>{label}</button>'
    f"</form>"
    for verb, label, enabled in buttons
  ) + "</div>"


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
      f'<pre>{esc(job["error"])}</pre></div>'
    )
  body = f'<pre>{esc(job["output"])}</pre>' if job["output"] else ""
  return f'<div class="notice"><b>{esc(job["verb"])}</b> finished.{body}</div>'


def issues_card(app):
  if not app.get("issues"):
    return ""
  items = "".join(
    f'<li>{esc(i["problem"])}'
    + (f'<span class="sub">{esc(i["fix"])}</span>' if i.get("fix") else "")
    + "</li>"
    for i in app["issues"]
  )
  return (
    f'<div class="error"><h2>Not ready to start</h2><ul>{items}</ul></div>'
  )


def config_row(entry, app_id):
  name = entry["name"]
  if entry["secret"]:
    hint = "set — type to replace" if entry["set"] else "not set"
    field = (
      f'<input type="password" name="set.{esc(name)}" '
      f'placeholder="{esc(hint)}" autocomplete="new-password">'
    )
  else:
    value = entry.get("value") or ""
    field = f'<input name="set.{esc(name)}" value="{esc(value)}">'
  note = entry.get("desc") or ""
  if not entry["set"] and entry.get("has_default"):
    note = (note + " " if note else "") + "(using the manifest default)"
  return (
    f'<tr><td class="key">{esc(name)}'
    f'{"<span class=sub>secret</span>" if entry["secret"] else ""}</td>'
    f'<td class="field"><form method="post" action="/apps/{quote(app_id)}" '
    f'class="cfg-edit">'
    f'<input type="hidden" name="action" value="config">'
    f"{field}"
    f'<button type="submit" class="cfg-save" disabled>Save</button>'
    f"</form></td>"
    f'<td class="muted wrap">{esc(note)}</td></tr>'
  )


def config_table(entries, app_id):
  rows = "".join(config_row(entry, app_id) for entry in entries)
  return f'<div class="scroll"><table class="kv"><tbody>{rows}</tbody></table></div>'


def config_form(app):
  entries = app.get("config") or []
  if not entries:
    return '<p class="empty">This app declares no configuration.</p>'
  app_id = app["app_id"]
  visible = [c for c in entries if not c.get("hidden")]
  hidden = [c for c in entries if c.get("hidden")]
  body = config_table(visible, app_id) if visible else ""
  if hidden:
    body += (
      '<details class="reveal">'
      "<summary>Show hidden configuration options</summary>"
      f"{config_table(hidden, app_id)}</details>"
    )
  return body


def volumes_section(app):
  volumes = app.get("volumes", [])
  if not volumes:
    return '<p class="empty">This app declares no volumes.</p>'
  options = app.get("options", {}).get("host_volumes", [])
  rows = []
  for volume in volumes:
    if volume["kind"] == "host":
      choices = "".join(
        f'<option value="{esc(tag)}"'
        f'{" selected" if tag == volume.get("bind") else ""}>{esc(tag)}</option>'
        for tag in options
      )
      cell = (
        f'<form method="post" action="/apps/{quote(app["app_id"])}" class="row">'
        f'<input type="hidden" name="action" value="bind">'
        f'<input type="hidden" name="volume" value="{esc(volume["name"])}">'
        f'<select name="tag"><option value="">(not bound)</option>{choices}'
        "</select><button type=submit>Bind</button></form>"
        if options
        else '<span class="muted">no host volumes declared yet</span>'
      )
    else:
      cell = f'<span class="muted">{esc(volume["path"] or "—")}</span>'
    rows.append(
      f'<tr><td class="key">{esc(volume["name"])}</td>'
      f'<td class="muted">{esc(volume["kind"])}'
      f'{"<span class=sub>read-only</span>" if volume["readonly"] else ""}</td>'
      f'<td class="path">{cell}</td>'
      f'<td class="muted">{fmt_size(volume["bytes"])}</td></tr>'
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Volume</th><th>Kind</th>'
    "<th>Where</th><th>Size</th></tr></thead><tbody>"
    + "".join(rows)
    + "</tbody></table></div>"
  )


def routes_section(app):
  routes = app.get("routes", [])
  if not routes:
    return '<p class="empty">This app publishes no routes.</p>'
  providers = app.get("options", {}).get("route_providers", [])
  rows = []
  for route in routes:
    choices = "".join(
      f'<option value="{esc(tag)}"'
      f'{" selected" if tag == route.get("provider") else ""}>{esc(tag)}</option>'
      for tag in providers
    )
    url = route.get("published_url") or route.get("url")
    rows.append(
      f'<tr><td class="key">{esc(route["name"])}</td>'
      f'<td class="muted">{esc(route["unit"])}:{esc(route["container_port"])}'
      f' &rarr; {esc(route["host_port"] or "unallocated")}</td>'
      f'<td class="path muted">{esc(url or "—")}</td>'
      f'<td><form method="post" action="/apps/{quote(app["app_id"])}" class="row">'
      f'<input type="hidden" name="action" value="route">'
      f'<input type="hidden" name="route" value="{esc(route["name"])}">'
      f"<select name=tag>{choices}</select>"
      "<button type=submit>Assign</button></form></td></tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Route</th><th>Port</th>'
    "<th>URL</th><th>Provider</th></tr></thead><tbody>"
    + "".join(rows)
    + "</tbody></table></div>"
  )


def unit_volumes_table(unit):
  volumes = unit.get("volumes") or []
  if not volumes:
    return '<p class="empty">No volumes mounted.</p>'
  rows = []
  for volume in volumes:
    rows.append(
      f'<tr><td class="key">{esc(volume["name"])}</td>'
      f'<td class="muted">{esc(volume["kind"])}'
      f'{"<span class=sub>read-only</span>" if volume["readonly"] else ""}</td>'
      f'<td class="muted path">{esc(volume["path"])}</td>'
      f'<td class="muted wrap">{esc(volume.get("desc") or "")}</td>'
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Volume</th><th>Kind</th>'
    "<th>Mounted at</th><th>Desc</th></tr></thead><tbody>"
    + "".join(rows)
    + "</tbody></table></div>"
  )


def units_section(app):
  blocks = []
  for unit in app.get("units", []):
    state = unit.get("state")
    dot = "running" if state == "running" else ("exited" if state else "")
    env = unit.get("environment") or {}
    env_block = (
      '<details class="reveal"><summary>Environment</summary>'
      f"{kv_table(sorted(env.items()))}</details>"
      if env
      else ""
    )
    command = " ".join(unit["command"]) if unit.get("command") else ""
    blocks.append(
      f'<div class="card pad">'
      f'<div class="row between"><b>{esc(unit["name"])}</b>'
      f'<span class="pill"><span class="dot {dot}"></span>'
      f'{esc(state or "not created")}</span></div>'
      f'<div class="muted mono">{esc(unit["image"])}</div>'
      + (f'<div class="muted mono">$ {esc(command)}</div>' if command else "")
      + f"<h3>Volumes</h3>{unit_volumes_table(unit)}"
      + env_block
      + "</div>"
    )
  return "".join(blocks) or '<p class="empty">No run units.</p>'


def app_page(app, job, notice):
  name = app.get("display_name") or app["app_id"]
  meta = app.get("metadata", {})
  skip = {"display_name", "description", "app_id"}
  pairs = [(k, v) for k, v in sorted(meta.items()) if k not in skip]
  return (
    notice
    + f'<div class="apphead"><div>{status_cell(app)}'
    f'<p class="lede">{esc(app.get("description") or "")}</p>'
    f'<p class="muted mono">{esc(app["app_id"])}</p></div>'
    f"{lifecycle_bar(app)}</div>"
    + job_card(job)
    + issues_card(app)
    + "<h2>Manifest</h2>"
    + (
      f'<div class="card">{kv_table(pairs)}</div>'
      if pairs
      else '<div class="card"><p class="empty">No extra metadata.</p></div>'
    )
    + "<h2>Configuration</h2>"
    + f'<div class="card">{config_form(app)}</div>'
    + "<h2>Volumes</h2>"
    + f'<div class="card">{volumes_section(app)}</div>'
    + "<h2>Routes</h2>"
    + f'<div class="card">{routes_section(app)}</div>'
    + "<h2>Run units</h2>"
    + units_section(app)
  )


def render(path, query=None, notice=""):
  """(title, body_html, version) for a nav path. Never raises."""
  query = query or {}
  try:
    version = api("/version").get("harbor", "")
  except ApiError as e:
    return "Dashboard" if path == "/" else path.strip("/").title(), error_card(e), ""

  if path == "/apps":
    try:
      return "Apps", apps_table(api("/apps").get("apps", [])), version
    except ApiError as e:
      return "Apps", error_card(e), version

  if path.startswith("/apps/"):
    app_id = unquote(path[len("/apps/"):])
    try:
      app = api(f"/apps/{quote(app_id)}")
      job = None
      if query.get("job"):
        try:
          job = api(f"/jobs/{quote(query['job'][0])}")
        except ApiError:
          job = None
      title = app.get("display_name") or app_id
      return title, app_page(app, job, notice), version
    except ApiError as e:
      return app_id, error_card(e), version

  if path == "/volumes":
    try:
      return "Volumes", volumes_page(query.get("sizes") == ["1"], notice), version
    except ApiError as e:
      return "Volumes", error_card(e), version

  if path == "/":
    return (
      "Dashboard",
      f'<div class="card"><p class="note">Hello. Connected to harbor '
      f"{esc(version)} over <code>{esc(where())}</code>.</p></div>",
      version,
    )

  title = path.strip("/").title()
  return (
    title,
    f'<div class="card"><p class="empty">{esc(title)} is not built yet.</p></div>',
    version,
  )


class Handler(BaseHTTPRequestHandler):
  server_version = "harbor-ui"

  def do_GET(self):
    parsed = urlparse(self.path)
    path = parsed.path.rstrip("/") or "/"
    if path not in dict(NAV) and not path.startswith("/apps/"):
      self.reply(404, page(path, "Not found", '<div class="card">'
                           '<p class="empty">No such page.</p></div>'))
      return
    query = parse_qs(parsed.query)
    # A notice survives exactly one redirect, in the query string, so a
    # refresh after an action does not repeat the action.
    notice = ""
    if query.get("ok"):
      notice = f'<div class="notice">{esc(query["ok"][0])}</div>'
    if query.get("err"):
      notice = f'<div class="error"><p>{esc(query["err"][0])}</p></div>'
    title, body, version = render(path, query, notice)
    self.reply(200, page(path, title, body, version))

  def do_POST(self):
    parsed = urlparse(self.path)
    path = parsed.path.rstrip("/") or "/"
    length = int(self.headers.get("Content-Length") or 0)
    form = parse_qs(self.rfile.read(length).decode()) if length else {}

    def field(name):
      return (form.get(name) or [""])[0].strip()

    if path.startswith("/apps/"):
      self.post_app(unquote(path[len("/apps/"):]), field, form)
      return
    if path != "/volumes":
      self.redirect(path)
      return
    try:
      if field("action") == "create":
        api(
          "/host-volumes",
          "POST",
          {
            "tag": field("tag"),
            "path": field("path"),
            "readonly": bool(form.get("readonly")),
            "require_mount": bool(form.get("require_mount")),
          },
        )
        self.redirect(f"/volumes?ok=Added+host+volume+{quote(field('tag'))}")
      elif field("action") == "delete":
        api(f"/host-volumes/{quote(field('tag'))}", "DELETE")
        self.redirect(f"/volumes?ok=Removed+host+volume+{quote(field('tag'))}")
      else:
        self.redirect("/volumes")
    except ApiError as e:
      self.redirect(f"/volumes?err={quote(str(e))}")

  def post_app(self, app_id, field, form):
    """One action per submit: a lifecycle verb, or one kind of config change."""
    here = f"/apps/{quote(app_id)}"
    action = field("action")
    try:
      if action in ("start", "stop", "stage"):
        job = api("/jobs", "POST", {"verb": action, "args": {"app": app_id}})
        self.redirect(f"{here}?job={quote(job['id'])}")
        return
      if action == "config":
        # Blank means "leave it alone" -- especially for secrets, whose
        # current value the UI never had in the first place.
        values = {
          key[len("set.") :]: (form[key] or [""])[0]
          for key in form
          if key.startswith("set.") and (form[key] or [""])[0].strip()
        }
        if not values:
          self.redirect(f"{here}?ok=Nothing+to+change")
          return
        api(f"{here}/config", "POST", {"set": values})
        self.redirect(f"{here}?ok=Saved+{quote(str(len(values)))}+value(s)")
        return
      if action == "bind":
        api(f"{here}/config", "POST", {"bind": {field("volume"): field("tag")}})
        self.redirect(f"{here}?ok=Bound+{quote(field('volume'))}")
        return
      if action == "route":
        api(f"{here}/config", "POST", {"route": {field("route"): field("tag")}})
        self.redirect(f"{here}?ok=Assigned+{quote(field('route'))}")
        return
      self.redirect(here)
    except ApiError as e:
      self.redirect(f"{here}?err={quote(str(e))}")

  def redirect(self, location):
    """Post/redirect/get: the browser lands on a GET, so a refresh re-reads
    rather than re-submitting."""
    self.send_response(303)
    self.send_header("Location", location)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def reply(self, status, body):
    raw = body.encode()
    self.send_response(status)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Content-Length", str(len(raw)))
    # A page is a photograph of harbor state; never let one be served twice.
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(raw)

  def log_message(self, fmt, *args):
    print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
  print(f"harbor-ui on :{PORT}, reading {where()}", flush=True)
  ThreadingHTTPServer(("", PORT), Handler).serve_forever()
