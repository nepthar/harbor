"""The Activity page: what harbor ran unattended, and what each run printed.

Container logs are docker's and stream through `harbor logs`; this page shows
the other stream -- job (and, later, cron) output that harbord filed under
`$harbor/var/logs`. The list is the activity index; each available run links to
its output file.
"""

from urllib.parse import quote

from api import api
from layout import esc


def _pill(status):
  dot = "running" if status == "ok" else "bad"
  return f'<span class="pill"><span class="dot {dot}"></span>{esc(status)}</span>'


def _duration(ms):
  return f"{ms / 1000:.1f}s" if ms is not None else "&mdash;"


def _rows(runs):
  if not runs:
    return (
      '<p class="empty">Nothing recorded yet. Runs land here as harbord '
      "executes jobs.</p>"
    )
  rows = []
  for run in runs:
    app = run["app_id"] or "harbor"
    if run["available"]:
      dirname, _, filename = run["log"].partition("/")
      output = f'<a href="/logs?app={quote(dirname)}&file={quote(filename)}">view</a>'
    else:
      output = '<span class="muted">pruned</span>'
    rows.append(
      "<tr>"
      f'<td class="muted"><time datetime="{esc(run["ts"])}">'
      f'{esc(run["ts"])}</time></td>'
      f'<td class="name">{esc(run["verb"])}</td>'
      f"<td>{esc(app)}</td>"
      f"<td>{_pill(run['status'])}</td>"
      f'<td class="muted">{_duration(run["duration_ms"])}</td>'
      f"<td>{output}</td>"
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>When</th><th>Verb</th>'
    "<th>App</th><th>Status</th><th>Took</th><th>Output</th></tr></thead>"
    "<tbody>" + "".join(rows) + "</tbody></table></div>"
  )


def list_page():
  runs = api("/activity?limit=50")["activity"]
  return (
    "<h2>Activity</h2>"
    + '<p class="lede">What harbor ran on your behalf &mdash; each job&rsquo;s '
    "output, kept as plain files under <code>$harbor/var/logs</code>. Container "
    "logs stay with docker: <code>harbor logs &lt;app&gt;</code> streams "
    "those.</p>"
    + f'<div class="card">{_rows(runs)}</div>'
  )


def detail_page(dirname, filename):
  record = api(f"/activity/{quote(dirname)}/{quote(filename)}")
  return (
    f'<h2>{esc(record["file"])} <span class="act">'
    '<a href="/logs">Back to activity</a></span></h2>'
    + f'<div class="card"><pre>{esc(record["text"])}</pre></div>'
  )


def page(app="", file=""):
  if app and file:
    return detail_page(app, file)
  return list_page()
