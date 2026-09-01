"""The Volumes page: host-volume declarations and harbor-managed storage."""

from api import api
from layout import esc, fmt_size, icon_button


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
      f'<td class="muted">{fmt_size(entry.get("bytes"))}</td>'
      f'<td class="act"><form method="post" action="/volumes"'
      f' data-confirm="Are you sure you want to delete the host volume '
      f"{esc(entry['tag'])}? Apps bound to it will fail to start until they "
      f"are bound somewhere else. Nothing under {esc(entry['path'])} is "
      f'touched.">'
      f'<input type="hidden" name="action" value="delete">'
      f'<input type="hidden" name="tag" value="{esc(entry["tag"])}">'
      f"{icon_button('delete', 'delete-outline', submit=True, danger=True)}</form></td>"
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Name</th><th>Path</th>'
    '<th>Flags</th><th>Size</th><th class="act"></th></tr></thead><tbody>'
    + "".join(rows)
    + "</tbody></table></div>"
  )


def host_volume_form():
  return (
    '<form method="post" action="/volumes" class="row">'
    '<input type="hidden" name="action" value="create">'
    '<input name="tag" placeholder="name (e.g. media)" required>'
    '<input name="path" placeholder="/mnt/media" required class="grow">'
    '<label><input type="checkbox" name="readonly"> read-only</label>'
    '<label><input type="checkbox" name="require_mount"> require mount</label>'
    f"{icon_button('add', 'plus-box-outline', submit=True)}</form>"
  )


def volume_rows(volumes):
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
    rows.append(
      "<tr>"
      f'<td class="name">{esc(volume["name"])}{orphan}</td>'
      f'<td class="muted">{esc(volume["app_id"])}</td>'
      f'<td class="muted">{esc(volume["kind"])}</td>'
      f"<td>{use}</td>"
      f'<td class="muted">{fmt_size(volume.get("bytes"))}</td>'
      "</tr>"
    )
  return (
    '<div class="scroll"><table><thead><tr><th>Volume</th><th>App</th>'
    "<th>Kind</th><th>Use</th><th>Size</th></tr></thead><tbody>"
    + "".join(rows)
    + "</tbody></table></div>"
  )


def harbor_rows(entries):
  """Whatever directories harbord names, in the order it names them."""
  if not entries:
    return '<p class="empty">Harbor reported no directories.</p>'
  rows = "".join(
    f'<tr><td class="name">{esc(entry.get("name"))}</td>'
    f'<td class="muted wrap">{esc(entry.get("description") or "")}</td>'
    f'<td class="muted">{fmt_size(entry.get("bytes"))}</td></tr>'
    for entry in entries
  )
  return (
    '<div class="scroll"><table><thead><tr><th>Directory</th>'
    "<th>Description</th><th>Size</th>"
    "</tr></thead><tbody>" + rows + "</tbody></table></div>"
  )


def volumes_page(notice=""):
  host = api("/host-volumes")["host_volumes"]
  body = api("/volumes")
  return (
    notice
    + "<h2>Host volumes</h2>"
    + '<p class="lede">Named volumes on this machine that apps may bind to</p>'
    + f'<div class="card">{host_volume_rows(host)}</div>'
    + f'<div class="card entry">{host_volume_form()}</div>'
    + "<h2>App volumes</h2>"
    + '<p class="lede">Harbor-managed application storage. Sizes are refreshed hourly.</p>'
    + f'<div class="card">{volume_rows(body.get("volumes") or [])}</div>'
    + "<h2>Harbor</h2>"
    + '<p class="lede">Harbor internal storage</p>'
    + f'<div class="card">{harbor_rows(body.get("harbor_dirs") or [])}</div>'
  )
