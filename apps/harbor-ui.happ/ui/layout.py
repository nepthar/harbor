"""The shell around every page: JS, nav, and shared HTML fragments.

The CSS is static/harbor.css; this module only links it.
"""

import html
import json
from hashlib import blake2s
from pathlib import Path

NAV = (
  ("/", "Dashboard"),
  ("/snapshots", "Snapshots"),
  ("/volumes", "Volumes"),
  ("/catalog", "Repos"),
  ("/logs", "Activity"),
)

# The stylesheet lives in static/harbor.css, served like any other asset.
# Pages themselves are `no-store`, but this file is not, so its URL carries a
# digest of its contents: change the CSS, reinstall, and the URL changes with
# it rather than a browser holding on to the old one.
_STATIC = Path(__file__).parent / "static"


def _asset_version(name):
  """Short digest of a static file. "dev" when it cannot be read."""
  try:
    return blake2s((_STATIC / name).read_bytes(), digest_size=6).hexdigest()
  except OSError:
    return "dev"


STYLE_HREF = f"/static/harbor.css?v={_asset_version('harbor.css')}"


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
  """Which nav entry a path belongs to.

  App detail pages have no nav entry of their own -- the list they belong to
  lives on the dashboard now -- so they light Dashboard instead of nothing.
  """
  if path.startswith("/apps"):
    return "/"
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
<link rel="stylesheet" href="{STYLE_HREF}"></head>
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
