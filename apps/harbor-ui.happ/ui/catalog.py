"""The Catalog page: listings, the app card, fetch preview, and updates."""

from urllib.parse import quote

from api import ApiError, api
from layout import error_card, esc, job_button, job_modal


def catalog_app_entry(catalogs, app_id):
  for catalog in catalogs:
    for app in catalog.get("apps") or []:
      if app.get("app_id") == app_id:
        return app
  return None


def catalog_tables(catalogs, open_app="", update=None, confirm=False):
  if not catalogs:
    return '<div class="card"><p class="empty">No catalogs configured.</p></div>'
  parts = []
  cards = []
  for catalog in catalogs:
    parts.append(f"<h2>{esc(catalog.get('name'))}</h2>")
    apps = catalog.get("apps") or []
    if not apps:
      parts.append(
        '<div class="card"><p class="empty">No apps in this catalog.</p></div>'
      )
      continue
    rows = []
    for app in apps:
      name = app.get("display_name") or app.get("app_id")
      version = app.get("version")
      app_id = app.get("app_id") or ""
      card_id = catalog_card_id(app)
      is_open = bool(open_app) and app_id == open_app
      card_app = dict(app)
      if is_open and update is not None:
        card_app["update"] = update
      cards.append(
        catalog_card(card_app, card_id, hidden=not is_open, confirm=is_open and confirm)
      )
      rows.append(
        f'<tr class="catalog-row" data-card="{esc(card_id)}">'
        f'<td class="name">{esc(name)}</td>'
        f'<td class="mono muted">{esc(app_id)}</td>'
        f'<td class="muted">{esc(version) if version else "&mdash;"}</td>'
        f'<td class="wrap">{esc(app.get("description") or "")}</td>'
        f'<td class="muted">{esc(app.get("source") or "local")}</td>'
        "</tr>"
      )
    parts.append(
      '<div class="card scroll"><table><thead><tr>'
      "<th>Name</th><th>App ID</th><th>Version</th>"
      "<th>Description</th><th>Source</th>"
      "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
  if cards:
    hidden = "" if open_app else " hidden"
    close = ' data-close="/catalog"' if open_app else ""
    parts.append(
      f'<div id="catalog-shade" class="shade"{close}{hidden}>'
      + "".join(cards)
      + "</div>"
    )
  return "".join(parts)


def fetch_bar(target=""):
  """Where an operator names a happ to fetch. GitHub is the only source today."""
  return (
    '<form class="fetchbar" method="get" action="/catalog">'
    '<input type="hidden" name="fetch" value="1">'
    '<input name="target" placeholder="github:user/repo/ref/path/name.happ" '
    f'value="{esc(target)}" autofocus>'
    '<button type="submit">Preview</button>'
    '<a class="link" href="/catalog">Cancel</a>'
    '<p class="hint">Only github: targets for now. Harbor shows you the '
    "manifest before anything is installed.</p>"
    "</form>"
  )


def fetch_preview(app):
  """The previewed happ, over the catalog, as the same card a row opens."""
  return (
    '<div id="fetch-shade" class="shade" data-close="/catalog?fetch=1">'
    + catalog_card(
      app, "card-fetch-preview", actions=preview_actions, hidden=False, status=False
    )
    + "</div>"
  )


def catalog_card_id(app):
  return f"card-{app.get('catalog') or 'apps'}--{app.get('app_id') or ''}"


def catalog_status_pill(app):
  """Installed-ness first, config second."""
  if app.get("configured") is None:
    return ""
  state = app.get("state")
  if state == "available":
    return '<span class="pill"><span class="dot"></span>not installed</span>'
  if state == "uninstalled":
    return '<span class="pill"><span class="dot exited"></span>uninstalled</span>'
  if app.get("configured") == "missing":
    return '<span class="pill"><span class="dot exited"></span>needs config</span>'
  return '<span class="pill"><span class="dot running"></span>ready</span>'


def catalog_stale_note(app):
  if not app.get("manifest_stale"):
    return ""
  return (
    '<p class="stale">The manifest shown here has changed since this app '
    "was installed. Re-install to pick up the new one.</p>"
  )


def catalog_conflict_note(app):
  if not app.get("conflict"):
    return ""
  return f'<p class="conflict">{esc(app["conflict"])}</p>'


def catalog_actions(app):
  """Reinstall from the catalog. Starting is the app page's job, not this one."""
  if app.get("configured") is None:
    return ""
  app_id = app.get("app_id") or ""
  label = "Re-install" if app.get("state") == "installed" else "Install"
  update = ""
  github = str(app.get("source") or "").startswith("github:")
  if github and (app.get("update") or {}).get("available"):
    update = (
      f'<form method="get" action="/catalog">'
      f'<input type="hidden" name="app" value="{esc(app_id)}">'
      f'<input type="hidden" name="confirm" value="1">'
      f'<button type="submit">Update</button></form>'
    )
  elif github and app.get("update") is None:
    update = (
      f'<form method="get" action="/catalog">'
      f'<input type="hidden" name="app" value="{esc(app_id)}">'
      f'<input type="hidden" name="check" value="1">'
      f'<button type="submit">Check for update</button></form>'
    )
  return (
    '<div class="row actions">'
    + job_button(
      label,
      "install",
      title=f"{label} {app.get('display_name') or app_id}",
      desc=(
        f"Installs {app_id} from this catalog entry so it can be started. "
        f"Any data and configuration it already has are kept."
      ),
      args={"app": app_id},
    )
    + f"{update}</div>"
  )


def confirm_update_actions(app):
  """Apply the remote copy the operator just reviewed, or back out."""
  app_id = app.get("app_id") or ""
  return (
    '<div class="row actions">'
    + job_button(
      "Apply update",
      "fetch",
      title=f"Update {app_id}",
      desc=(
        f"Replaces the catalog copy of {app_id} with the remote one reviewed "
        f"above. The installed app is unchanged until it is reinstalled."
      ),
      args={"target": app_id},
      autorun=True,
    )
    + f'<a class="link" href="/catalog?app={quote(app_id)}">Cancel</a>'
    + "</div>"
  )


def catalog_update_section(app, confirm=False):
  """Remote vs fetched, under the name/version/status block."""
  update = app.get("update")
  if not update:
    return ""
  if update.get("error"):
    return f'<p class="conflict">{esc(update["error"])}</p>'
  if update.get("pinned"):
    ver = update.get("current_version") or ""
    sha = (update.get("current_sha") or "")[:8]
    return (
      f'<div class="update"><p>Pinned at {esc(ver)} '
      f'<span class="mono muted">{esc(sha)}</span>. '
      f"This happ will not follow its branch.</p></div>"
    )
  if not update.get("available"):
    return '<div class="update"><p class="muted">Up to date.</p></div>'
  cur_v = update.get("current_version") or ""
  new_v = update.get("remote_version") or ""
  cur_s = (update.get("current_sha") or "")[:8]
  new_s = (update.get("remote_sha") or "")[:8]
  prompt = ""
  if confirm:
    prompt = (
      "<p>This replaces the catalog copy. The running app is unchanged "
      "until you stop it, Re-install, and Start.</p>"
    )
  return (
    f'<div class="update">'
    f"<p><b>Update available</b></p>"
    f'<p class="mono">{esc(cur_v)} → {esc(new_v)}</p>'
    f'<p class="mono muted">{esc(cur_s)} → {esc(new_s)}</p>'
    f"{prompt}</div>"
  )


def catalog_diff_html(diff):
  """The remote manifest as a unified diff, colored in the right-hand pane."""
  if not diff:
    return (
      '<pre class="app-card-manifest app-card-diff">'
      '<span class="diff-hunk">The manifest is unchanged; other files differ.'
      "</span></pre>"
    )
  parts = []
  for line in diff.splitlines():
    if line.startswith("+") and not line.startswith("+++"):
      cls = "diff-add"
    elif line.startswith("-") and not line.startswith("---"):
      cls = "diff-del"
    elif line.startswith("@") or line.startswith("+++") or line.startswith("---"):
      cls = "diff-hunk"
    else:
      cls = "diff-ctx"
    parts.append(f'<span class="{cls}">{esc(line) or " "}</span>')
  return '<pre class="app-card-manifest app-card-diff">' + "".join(parts) + "</pre>"


def preview_actions(app):
  """Fetch what the preview just showed, or nothing when the id is taken."""
  close = '<a class="link" href="/catalog?fetch=1">Back</a>'
  if app.get("conflict"):
    return f'<div class="row actions">{close}</div>'
  target = app.get("target") or ""
  return (
    '<div class="row actions">'
    + job_button(
      "Fetch",
      "fetch",
      title=f"Fetch {app.get('app_id') or target}",
      desc=f"Downloads {target} into the catalog. Nothing is installed or started.",
      args={"target": target, "yes": "1"},
      autorun=True,
      done="/catalog",
    )
    + f"{close}</div>"
  )


def catalog_card(
  app, card_id, actions=catalog_actions, hidden=True, status=True, confirm=False
):
  """One happ, full width: what it is on the left, its manifest on the right.

  `actions` is the only thing that differs between a catalog entry and a
  fetch preview. A preview renders open and without a status pill.
  """
  name = app.get("display_name") or app.get("app_id")
  version = app.get("version")
  pill = catalog_status_pill(app) if status else ""
  ver = f'<span class="muted">v{esc(version)}</span>' if version else ""
  side = " ".join(p for p in (ver, pill) if p)
  if app.get("configured") is None:
    summary = '<p class="lede">This happ\'s manifest could not be read.</p>'
  else:
    summary = (
      f'<p class="lede">{esc(app.get("description") or "")}</p>'
      f'<p class="muted mono">{esc(app.get("app_id"))}'
      f" · {esc(app.get('source') or 'local')}</p>"
    )
  show_diff = confirm and (app.get("update") or {}).get("available")
  if show_diff:
    acts = confirm_update_actions(app)
    pane = catalog_diff_html((app.get("update") or {}).get("diff") or "")
  else:
    acts = actions(app)
    manifest = esc(app.get("manifest") or "")
    pane = (
      f'<pre class="app-card-manifest">'
      f'<code class="language-toml">{manifest}</code></pre>'
    )
  return (
    f'<article class="app-card" id="{esc(card_id)}"{" hidden" if hidden else ""}>'
    f'<div class="app-card-head">'
    f'<div class="app-card-intro">'
    f'<div class="row between"><h2>{esc(name)}</h2>{side}</div>'
    f"{summary}{catalog_conflict_note(app)}{catalog_stale_note(app)}"
    f"{catalog_update_section(app, confirm)}</div>"
    f"{acts}</div>"
    f"{pane}"
    "</article>"
  )


def page(
  version,
  notice="",
  *,
  fetch=False,
  target="",
  app="",
  confirm=False,
  check=False,
):
  """The catalog listing, plus fetch preview / update check when asked."""
  target = target.strip()
  open_app = app.strip()
  body = notice
  if fetch or target:
    body += fetch_bar(target)
  # The preview is its own request and its own failure: a target that does
  # not resolve must not take the catalog listing down with it.
  preview = ""
  if target:
    try:
      preview = fetch_preview(
        api("/catalog/preview", "POST", {"target": target}, timeout=60)
      )
    except ApiError as e:
      body += f'<div class="error"><p>{esc(e)}</p></div>'
  try:
    catalogs = api("/catalog").get("catalogs", [])
  except ApiError as e:
    return "Catalog", error_card(e), version
  update = None
  # Opening a card is cheap. GitHub is not: only Check / Update wait on it.
  if open_app and (check or confirm):
    entry = catalog_app_entry(catalogs, open_app)
    if entry and str(entry.get("source") or "").startswith("github:"):
      try:
        update = api("/catalog/check", "POST", {"app": open_app}, timeout=60)
      except ApiError as e:
        update = {"error": str(e)}
  body += catalog_tables(catalogs, open_app=open_app, update=update, confirm=confirm)
  return "Catalog", body + preview + job_modal(), version
