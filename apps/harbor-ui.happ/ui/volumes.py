"""The Volumes page: host-volume declarations and harbor-managed storage."""

from api import api
from layout import esc, fmt_size


def host_volume_rows(entries):
  if not entries:
    return '<p class="empty">No host volumes declared yet.</p>'
  rows = []
  for entry in entries:
    missing = "" if entry["exists"] else '<span class="sub">path is missing</span>'
    flags = ", ".join(
      flag
      for flag, on in (
        ("read-only", entry["readonly"]),
        ("require mount", entry["require_mount"]),
      )
      if on
    )
    rows.append(
      "<tr>"
      f'<td class="name">{esc(entry["tag"])}</td>'
      f'<td class="muted path">{esc(entry["path"])}{missing}</td>'
      f'<td class="muted">{esc(flags or "—")}</td>'
      f'<td class="act"><form method="post" action="/volumes"'
      f' data-confirm="Are you sure you want to delete the host volume '
      f"{esc(entry['tag'])}? Apps bound to it will fail to start until they "
      f"are bound somewhere else. Nothing under {esc(entry['path'])} is "
      f'touched.">'
      f'<input type="hidden" name="action" value="delete">'
      f'<input type="hidden" name="tag" value="{esc(entry["tag"])}">'
      f'<button type="submit">Delete</button></form></td>'
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Tag</th><th>Path</th>'
    '<th>Flags</th><th class="act"></th></tr></thead><tbody>'
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
    + "<h2>Host volumes</h2>"
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
