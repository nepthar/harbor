"""The Apps list and the single-app detail page."""

from urllib.parse import quote, unquote

from api import ApiError, api
from layout import (
  error_card,
  esc,
  fmt_size,
  job_button,
  job_modal,
  kv_table,
)


def status_cell(app):
  """Containers, unless there is no installation for them to belong to."""
  if app.get("state") == "uninstalled":
    return (
      '<span class="pill"><span class="dot exited"></span>uninstalled</span>'
      '<span class="sub">data and config kept</span>'
    )
  status = app.get("status") or "unknown"
  containers = app.get("containers") or {}
  running, total = containers.get("running", 0), containers.get("total", 0)
  css = status if status in ("running", "exited") else ""
  detail = f"{running}/{total}" if total else "no containers"
  return (
    f'<span class="pill"><span class="dot {css}"></span>{esc(status)}</span>'
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


def lifecycle_bar(app):
  """Start or stop, then reload, snapshot, uninstall. Each opens the job modal."""
  app_id = app["app_id"]
  name = app.get("display_name") or app_id
  running = app["status"] == "running"
  installed = app.get("state") == "installed"
  # Exactly one of start/stop ever applies, so only that one is drawn, and it
  # carries a word rather than a glyph -- it is the verb the page is for.
  primary = (
    job_button(
      "Stop",
      "stop",
      title=f"Stop {name}",
      desc=f"Stops {app_id}'s containers. Its data and configuration are untouched.",
      args={"app": app_id},
    )
    if running
    else job_button(
      "Start",
      "start",
      title=f"Start {name}",
      desc=f"Starts {app_id}, installing it first if it is not installed yet.",
      args={"app": app_id},
    )
  )
  buttons = [
    primary,
    job_button(
      "Reload",
      "reload",
      title=f"Reload {name}",
      desc=(
        f"Stops {app_id} if it is running, rebuilds its installation from the "
        f"catalog copy (pending configuration and a changed manifest included), "
        f"then starts it again if it was running. Data and its address are kept."
      ),
      args={"app": app_id},
    ),
    job_button(
      "Snapshot",
      "snapshot",
      title=f"Snapshot {name}",
      desc=(
        f"Copies {app_id}'s volumes and run state into an archive under "
        f"snapshots/. The app is stopped for the copy and started again after."
      ),
      args={"app": app_id},
      fields=[{"name": "label", "placeholder": "label (optional)"}],
      enabled=installed,
    ),
    job_button(
      "Remove",
      title=f"Remove {name}",
      desc=(
        f"Pick how much of {app_id} to remove. To keep a copy of the data "
        f"first, close this and take a Snapshot."
      ),
      choices=[
        {
          "label": "Uninstall",
          "verb": "uninstall",
          "args": {"app": app_id},
          "desc": (
            "Removes the installation. Data, configuration, secrets and its "
            "address are kept, so reinstalling picks up where it left off."
          ),
        },
        {
          "label": "Reset",
          "verb": "reset",
          "args": {"app": app_id},
          "desc": (
            "Deletes the data volumes and installs the app again from the "
            "bundle. Configuration and address are kept."
          ),
        },
        {
          "label": "Uninstall and purge",
          "verb": "uninstall",
          "args": {"app": app_id, "purge": "1"},
          "desc": (
            "Removes everything harbor holds: the installation, the data "
            "volumes, the configuration and secrets, and its address. The "
            "catalog copy survives."
          ),
        },
      ],
      danger=True,
    ),
  ]
  return '<div class="row actions">' + "".join(buttons) + "</div>"


def pending_card(app):
  """Config written since the running containers were started."""
  if not app.get("config_pending"):
    return ""
  return (
    '<div class="notice"><b>Restart to apply pending configuration changes.</b>'
    '<span class="sub">Settings and route assignments were changed after '
    "this app was started, so what is running does not have them yet.</span>"
    "</div>"
  )


def issues_card(app):
  if not app.get("issues"):
    return ""
  items = "".join(
    f"<li>{esc(i['problem'])}"
    + (f'<span class="sub">{esc(i["fix"])}</span>' if i.get("fix") else "")
    + "</li>"
    for i in app["issues"]
  )
  return f'<div class="error"><h2>Not ready to start</h2><ul>{items}</ul></div>'


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
    f"{'<span class=sub>secret</span>' if entry['secret'] else ''}</td>"
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
  basic = [c for c in entries if not c.get("advanced")]
  advanced = [c for c in entries if c.get("advanced")]
  body = config_table(basic, app_id) if basic else ""
  if advanced:
    body += (
      '<details class="reveal">'
      "<summary>Show advanced configuration options</summary>"
      f"{config_table(advanced, app_id)}</details>"
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
        f"{' selected' if tag == volume.get('bind') else ''}>{esc(tag)}</option>"
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
      f"{'<span class=sub>read-only</span>' if volume['readonly'] else ''}</td>"
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
      f"{' selected' if tag == route.get('provider') else ''}>{esc(tag)}</option>"
      for tag in providers
    )
    url = route.get("published_url") or route.get("url")
    url_cell = (
      f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(url)}</a>'
      if url
      else "—"
    )
    rows.append(
      f'<tr><td class="key">{esc(route["name"])}</td>'
      f'<td class="muted">{esc(route["unit"])}:{esc(route["container_port"])}'
      f" &rarr; {esc(route['host_port'] or 'unallocated')}</td>"
      f'<td class="path muted">{url_cell}</td>'
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


def commands_section(app):
  """A Run button per manifest `[commands]` entry, opening a modal that posts
  a `cmd` job and tails its activity file.
  """
  commands = app.get("commands") or []
  if not commands:
    return '<p class="empty">This app declares no commands.</p>'
  installed = app.get("state") == "installed"
  rows = []
  for command in commands:
    desc = command.get("desc") or ""
    button = job_button(
      "Run",
      "cmd",
      title=f"{app['app_id']}: {command['name']}",
      desc=desc
      or f"Runs the {command['name']!r} command declared in this happ's manifest.",
      args={"app": app["app_id"], "command": command["name"]},
      fields=[{"name": "args", "placeholder": "extra arguments (optional)"}],
      enabled=installed,
    )
    rows.append(
      f'<tr><td class="key">{esc(command["name"])}</td>'
      f'<td class="muted wrap">{esc(desc)}</td>'
      f'<td class="muted">{esc(command["unit"])}</td>'
      f'<td class="act">{button}</td></tr>'
    )
  table = (
    '<div class="scroll"><table><thead><tr><th>Command</th><th>Description</th>'
    '<th>Unit</th><th class="act"></th></tr></thead><tbody>'
    + "".join(rows)
    + "</tbody></table></div>"
  )
  return table


def unit_volumes_table(unit):
  volumes = unit.get("volumes") or []
  if not volumes:
    return '<p class="empty">No volumes mounted.</p>'
  rows = []
  for volume in volumes:
    rows.append(
      f'<tr><td class="key">{esc(volume["name"])}</td>'
      f'<td class="muted">{esc(volume["kind"])}'
      f"{'<span class=sub>read-only</span>' if volume['readonly'] else ''}</td>"
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
      f"{esc(state or 'not created')}</span></div>"
      f'<div class="muted mono">{esc(unit["image"])}</div>'
      + (f'<div class="muted mono">$ {esc(command)}</div>' if command else "")
      + f"<h3>Volumes</h3>{unit_volumes_table(unit)}"
      + env_block
      + "</div>"
    )
  return "".join(blocks) or '<p class="empty">No run units.</p>'


def app_page(app, notice=""):
  meta = app.get("metadata", {})
  skip = {"display_name", "description", "app_id"}
  pairs = [(k, v) for k, v in sorted(meta.items()) if k not in skip]
  return (
    notice + f'<div class="apphead">{status_cell(app)}'
    f'<p class="lede">{esc(app.get("description") or "")}</p>'
    f'<p class="muted mono">{esc(app["app_id"])}</p></div>'
    + pending_card(app)
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
    + "<h2>Commands</h2>"
    + f'<div class="card">{commands_section(app)}</div>'
    + "<h2>Run units</h2>"
    + units_section(app)
    + job_modal()
  )


def list_page(version):
  try:
    return "Apps", apps_table(api("/apps").get("apps", [])), version
  except ApiError as e:
    return "Apps", error_card(e), version


def detail_page(app_id, version, notice=""):
  app_id = unquote(app_id)
  try:
    app = api(f"/apps/{quote(app_id)}")
    title = app.get("display_name") or app_id
    return (
      title,
      app_page(app, notice),
      version,
      (f'<span class="head-actions">{lifecycle_bar(app)}</span>'),
    )
  except ApiError as e:
    return app_id, error_card(e), version, ""
