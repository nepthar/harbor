"""The Catalog page: the repos apps come from, and the happs in each of them."""

from api import ApiError, api
from layout import error_card, esc, job_button, job_modal


def catalog_app_entry(catalogs, app_id):
  for catalog in catalogs:
    for app in catalog.get("apps") or []:
      if app.get("app_id") == app_id:
        return app
  return None


def add_repo_button():
  """The page-level action: subscribe to a folder of happs on GitHub."""
  return job_button(
    "+ Add Repo",
    "repo-add",
    title="Add a repo",
    desc=(
      "Harbor mirrors the folder and every happ in it becomes part of your "
      "catalog. Nothing is installed or started."
    ),
    fields=[
      {"name": "url", "placeholder": "github://user/repo/branch/folder"},
      {"name": "name", "placeholder": "name (optional)"},
    ],
    done="/catalog",
  )


def repo_actions(repo):
  """Update and remove, beside the repo's heading."""
  name = repo.get("name") or ""
  if not repo.get("removable"):
    return ""
  buttons = []
  if repo.get("url"):
    buttons.append(
      job_button(
        "Update",
        "repo-update",
        title=f"Update {name}",
        desc=(
          f"Re-reads {repo.get('url')} and replaces the local copy with what "
          f"it holds now. Installed apps keep running until you re-install "
          f"them."
        ),
        args={"name": name},
        autorun=True,
        done="/catalog",
      )
    )
  bound = repo.get("bound_apps") or []
  warning = ""
  if bound:
    warning = (
      f" {len(bound)} installed app(s) came from it "
      f"({', '.join(bound)}); they keep running, but harbor will stop "
      f"seeing updates for them."
    )
  buttons.append(
    job_button(
      "Remove",
      "repo-remove",
      title=f"Remove {name}",
      desc=(
        f"Drops {name} from the catalog and deletes its mirrored copy."
        f"{warning}"
      ),
      args={"name": name},
      danger=True,
      done="/catalog",
    )
  )
  return '<span class="act">' + "".join(buttons) + "</span>"


def repo_meta(repo):
  """Where a repo comes from, and when it was last mirrored."""
  bits = [f'<span class="mono">{esc(repo.get("location"))}</span>']
  sha = repo.get("sha")
  if sha:
    bits.append(f'<span class="mono">{esc(sha[:8])}</span>')
  at = repo.get("updated_at")
  if at:
    bits.append(f'<time datetime="{esc(at)}">{esc(at)}</time>')
  if not repo.get("exists"):
    bits.append('<span class="warnish">not mirrored yet</span>')
  return '<p class="lede repo-meta">' + " · ".join(bits) + "</p>"


def contested_note(contested):
  """Ids more than one repo carries."""
  if not contested:
    return ""
  items = "".join(
    f"<li><span class=mono>{esc(app_id)}</span> is in "
    f"{', '.join(esc(r) for r in repos)} — install it as "
    f"<span class=mono>{esc(app_id)}@&lt;repo&gt;</span></li>"
    for app_id, repos in sorted(contested.items())
  )
  return (
    f'<div class="notice contested"><b>Some app ids are in more than one '
    f"repo.</b><ul>{items}</ul></div>"
  )


def catalog_tables(catalogs, repos, contested, open_app=""):
  by_name = {repo.get("name"): repo for repo in repos}
  if not catalogs:
    return '<div class="card"><p class="empty">No repos configured.</p></div>'
  parts = []
  cards = []
  for catalog in catalogs:
    name = catalog.get("name")
    repo = by_name.get(name, {})
    parts.append(f"<h2>{esc(name)}{repo_actions(repo)}</h2>")
    parts.append(repo_meta(repo))
    apps = catalog.get("apps") or []
    if not apps:
      parts.append('<div class="card"><p class="empty">No happs here.</p></div>')
      continue
    rows = []
    for app in apps:
      display = app.get("display_name") or app.get("app_id")
      version = app.get("version")
      app_id = app.get("app_id") or ""
      card_id = catalog_card_id(app)
      is_open = bool(open_app) and app_id == open_app
      card_app = dict(app)
      card_app["contested"] = contested.get(app_id) or []
      cards.append(catalog_card(card_app, card_id, hidden=not is_open))
      mark = ""
      if app_id in contested:
        mark = ' <span class="dot exited" title="in more than one repo"></span>'
      rows.append(
        f'<tr class="catalog-row" data-card="{esc(card_id)}">'
        f'<td class="name">{esc(display)}{mark}</td>'
        f'<td class="muted">{esc(app_id)}</td>'
        f'<td class="muted">{esc(version) if version else "&mdash;"}</td>'
        f'<td class="wrap">{esc(app.get("description") or "")}</td>'
        "</tr>"
      )
    parts.append(
      '<div class="card scroll"><table><thead><tr>'
      "<th>Name</th><th>App ID</th><th>Version</th><th>Description</th>"
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


def catalog_card_id(app):
  return f"card-{app.get('repo') or 'main'}--{app.get('app_id') or ''}"


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


def catalog_actions(app):
  """Install from this repo. Starting is the app page's job."""
  if app.get("configured") is None:
    return ""
  app_id = app.get("app_id") or ""
  repo = app.get("repo") or ""
  label = "Re-install" if app.get("state") == "installed" else "Install"
  # Opening this card is the choice of repo, so it is always named.
  target = f"{app_id}@{repo}" if repo else app_id
  return (
    '<div class="row actions">'
    + job_button(
      label,
      "install",
      title=f"{label} {app.get('display_name') or app_id}",
      desc=(
        f"Installs {app_id} from {repo} so it can be started. Any data and "
        f"configuration it already has are kept."
      ),
      args={"app": target},
      done="/catalog",
    )
    + "</div>"
  )


def catalog_card(app, card_id, hidden=True):
  """One happ, full width: what it is on the left, its manifest on the right."""
  name = app.get("display_name") or app.get("app_id")
  version = app.get("version")
  pill = catalog_status_pill(app)
  ver = f'<span class="muted">v{esc(version)}</span>' if version else ""
  side = " ".join(p for p in (ver, pill) if p)
  if app.get("configured") is None:
    summary = '<p class="lede">This happ\'s manifest could not be read.</p>'
  else:
    summary = (
      f'<p class="lede">{esc(app.get("description") or "")}</p>'
      f'<p class="muted mono">{esc(app.get("app_id"))}'
      f" · {esc(app.get('repo') or 'main')}</p>"
    )
  others = [r for r in (app.get("contested") or []) if r != app.get("repo")]
  contested = ""
  if others:
    contested = (
      f'<p class="stale">Also carried by {", ".join(esc(r) for r in others)}. '
      f"Installing from here binds it to {esc(app.get('repo'))}.</p>"
    )
  manifest = esc(app.get("manifest") or "")
  return (
    f'<article class="app-card" id="{esc(card_id)}"{" hidden" if hidden else ""}>'
    f'<div class="app-card-head">'
    f'<div class="app-card-intro">'
    f'<div class="row between"><h2>{esc(name)}</h2>{side}</div>'
    f"{summary}{contested}{catalog_stale_note(app)}</div>"
    f"{catalog_actions(app)}</div>"
    f'<pre class="app-card-manifest">'
    f'<code class="language-toml">{manifest}</code></pre>'
    "</article>"
  )


def page(version, notice="", *, app=""):
  """Every repo, and the happs each one carries."""
  open_app = app.strip()
  actions = f'<span class="head-actions">{add_repo_button()}</span>'
  try:
    body = api("/catalog")
    repos = api("/repos").get("repos", [])
  except ApiError as e:
    return "Catalog", error_card(e) + job_modal(), version, actions
  catalogs = body.get("catalogs", [])
  contested = body.get("contested", {})
  return (
    "Catalog",
    notice
    + contested_note(contested)
    + catalog_tables(catalogs, repos, contested, open_app=open_app)
    + job_modal(),
    version,
    actions,
  )
