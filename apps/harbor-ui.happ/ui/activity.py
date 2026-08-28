"""The Activity page: what harbor ran unattended, and what each run printed.

Container logs are docker's and stream through `harbor logs`; this page shows
the other stream, filed under `$harbor/var/logs`.
"""

from api import api
from layout import esc, job_modal, log_button


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
      output = log_button(
        "view",
        run["log"],
        run["status"],
        title=f"{run['verb']} {app}".strip(),
      )
    else:
      output = '<span class="muted">pruned</span>'
    rows.append(
      "<tr>"
      f'<td class="muted"><time datetime="{esc(run["ts"])}">'
      f"{esc(run['ts'])}</time></td>"
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
  runs = api("/activity?limit=100")["activity"]
  return (
    "<h2>Activity</h2>"
    + '<p class="lede">Recent app-related activities and output</p>'
    + f'<div class="card">{_rows(runs)}</div>' + job_modal()
  )


def page():
  return list_page()
