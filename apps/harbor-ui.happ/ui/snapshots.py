"""The Snapshots page: every archive, and a restore button on each row."""

from api import api
from layout import esc, fmt_size, job_button, job_modal


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
      f'<td class="act">'
      + job_button(
        "Restore",
        "restore",
        title=f"Restore {snap['app_id']}",
        desc=(
          f"Replaces {snap['app_id']}'s run state, configuration and data "
          f"volumes with {snap['name']}. A pre-restore snapshot is taken first "
          f"when there is something to overwrite."
        ),
        args={"app": snap["app_id"], "snapshot": snap["name"]},
      )
      + "</td>"
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>App</th><th>When</th>'
    '<th>Tag</th><th>Size</th><th class="act"></th></tr></thead><tbody>'
    + "".join(rows)
    + "</tbody></table></div>"
  )


def page(notice=""):
  body = notice
  snapshots = api("/snapshots")["snapshots"]
  return (
    body + '<p class="lede">Archives under <code>snapshots/</code>. Restore '
    "replaces the app&rsquo;s current run state; a pre-restore snapshot is "
    "taken first when there is something to overwrite.</p>"
    + f'<div class="card">{_rows(snapshots)}</div>'
    + job_modal()
  )
