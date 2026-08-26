"""The Snapshots page: every archive, and a restore button on each row."""

from urllib.parse import quote

from api import ApiError, api
from layout import esc, fmt_size, job_card


def _when(snap):
  taken = snap.get("taken_at")
  if taken:
    return f'<time datetime="{esc(taken)}">{esc(taken)}</time>'
  return esc(snap["name"])


def _rows(snapshots):
  if not snapshots:
    return '<p class="empty">No snapshots yet. Take one from an app\'s page.</p>'
  rows = []
  for snap in snapshots:
    rows.append(
      "<tr>"
      f'<td class="name">{esc(snap["app_id"])}</td>'
      f'<td class="muted">{_when(snap)}</td>'
      f'<td class="muted">{esc(snap.get("tag") or "")}</td>'
      f'<td class="muted">{fmt_size(snap.get("bytes"))}</td>'
      f'<td class="act"><form method="post" action="/snapshots">'
      f'<input type="hidden" name="app" value="{esc(snap["app_id"])}">'
      f'<input type="hidden" name="snapshot" value="{esc(snap["name"])}">'
      '<button type="submit">Restore</button></form></td>'
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>App</th><th>When</th>'
    '<th>Tag</th><th>Size</th><th class="act"></th></tr></thead><tbody>'
    + "".join(rows)
    + "</tbody></table></div>"
  )


def page(notice="", job=""):
  body = notice
  if job:
    try:
      body += job_card(api(f"/jobs/{quote(job)}"))
    except ApiError:
      pass
  snapshots = api("/snapshots")["snapshots"]
  return (
    body + '<p class="lede">Archives under <code>snapshots/</code>. Restore '
    "replaces the app&rsquo;s current run state; a pre-restore snapshot is "
    "taken first when there is something to overwrite.</p>"
    + f'<div class="card">{_rows(snapshots)}</div>'
  )
