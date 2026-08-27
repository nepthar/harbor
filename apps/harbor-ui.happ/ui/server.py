"""HTTP front door: route a request to a page, or run a POST as a harbor verb."""

from urllib.parse import quote, unquote

import activity
import catalog
import installed
import snapshots
import volumes
from api import ApiError, api, where
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from layout import error_card, esc, page

app = FastAPI()

NO_STORE = {"Cache-Control": "no-store"}


def html(path, title, body, version="", status_code=200):
  return HTMLResponse(
    page(path, title, body, version),
    status_code=status_code,
    headers=NO_STORE,
  )


def see(location):
  """Post/redirect/get: the browser lands on a GET, so a refresh re-reads
  rather than re-submitting."""
  return RedirectResponse(location, status_code=303)


def banner(ok=None, err=None):
  notice = ""
  if ok:
    notice = f'<div class="notice">{esc(ok)}</div>'
  if err:
    notice = f'<div class="error"><p>{esc(err)}</p></div>'
  return notice


def harbor_version(path, title):
  """Harbor version, or an HTML error page if harbord is unreachable."""
  try:
    return api("/version").get("harbor", ""), None
  except ApiError as e:
    return "", html(path, title, error_card(e))


def field(form, name):
  value = form.get(name)
  return "" if value is None else str(value).strip()


@app.get("/")
def dashboard():
  version, err = harbor_version("/", "Dashboard")
  if err:
    return err
  return html(
    "/",
    "Dashboard",
    f'<div class="card"><p class="note">Hello. Connected to harbor '
    f"{esc(version)} over <code>{esc(where())}</code>.</p></div>",
    version,
  )


@app.get("/apps")
def apps_list():
  version, err = harbor_version("/apps", "Apps")
  if err:
    return err
  title, body, version = installed.list_page(version)
  return html("/apps", title, body, version)


@app.get("/snapshots")
def snapshots_get(job: str = "", ok: str | None = None, err: str | None = None):
  version, unreachable = harbor_version("/snapshots", "Snapshots")
  if unreachable:
    return unreachable
  try:
    body = snapshots.page(banner(ok, err), job)
  except ApiError as e:
    return html("/snapshots", "Snapshots", error_card(e), version)
  return html("/snapshots", "Snapshots", body, version)


@app.post("/snapshots")
async def post_snapshots(request: Request):
  form = await request.form()
  app_id = field(form, "app")
  name = field(form, "snapshot")
  if not app_id or not name:
    return see("/snapshots")
  try:
    job = api(
      "/jobs",
      "POST",
      {"verb": "restore", "args": {"app": app_id, "snapshot": name}},
    )
    return see(f"/snapshots?job={quote(job['id'])}")
  except ApiError as e:
    return see(f"/snapshots?err={quote(str(e))}")


@app.get("/apps/{app_id}")
def app_detail(
  app_id: str, job: str = "", ok: str | None = None, err: str | None = None
):
  version, unreachable = harbor_version(f"/apps/{app_id}", "Apps")
  if unreachable:
    return unreachable
  title, body, version = installed.detail_page(
    app_id, version, notice=banner(ok, err), job=job
  )
  return html(f"/apps/{app_id}", title, body, version)


@app.get("/volumes")
def volumes_get(
  sizes: str | None = None, ok: str | None = None, err: str | None = None
):
  version, unreachable = harbor_version("/volumes", "Volumes")
  if unreachable:
    return unreachable
  try:
    body = volumes.volumes_page(sizes == "1", banner(ok, err))
  except ApiError as e:
    return html("/volumes", "Volumes", error_card(e), version)
  return html("/volumes", "Volumes", body, version)


@app.get("/catalog")
def catalog_get(
  fetch: str | None = None,
  target: str = "",
  app: str = "",
  confirm: str | None = None,
  check: str | None = None,
  job: str = "",
  ok: str | None = None,
  err: str | None = None,
):
  version, unreachable = harbor_version("/catalog", "Catalog")
  if unreachable:
    return unreachable
  title, body, version = catalog.page(
    version,
    banner(ok, err),
    fetch=fetch is not None,
    target=target,
    app=app,
    confirm=confirm == "1",
    check=check == "1",
    job=job,
  )
  return html("/catalog", title, body, version)


@app.get("/logs")
def logs(file: str = ""):
  version, unreachable = harbor_version("/logs", "Activity")
  if unreachable:
    return unreachable
  try:
    body = activity.page(file)
  except ApiError as e:
    return html("/logs", "Activity", error_card(e), version)
  return html("/logs", "Activity", body, version)


@app.post("/catalog")
async def post_catalog(request: Request):
  """Fetch a previewed target, or update an already-fetched app id.

  `yes` is this submit for a first install: the operator has now read the
  manifest the preview put in front of them. An update has no prompt in
  harbor itself -- the confirm step is this page's.
  """
  form = await request.form()
  target = field(form, "target")
  if field(form, "action") != "fetch" or not target:
    return see("/catalog")
  args = {"target": target}
  if target.startswith("github:"):
    args["yes"] = "1"
  try:
    job = api("/jobs", "POST", {"verb": "fetch", "args": args})
    if target.startswith("github:"):
      return see(f"/catalog?job={quote(job['id'])}")
    return see(f"/catalog?app={quote(target)}&job={quote(job['id'])}")
  except ApiError as e:
    if target.startswith("github:"):
      return see(f"/catalog?fetch=1&err={quote(str(e))}")
    return see(f"/catalog?app={quote(target)}&err={quote(str(e))}")


@app.post("/volumes")
async def post_volumes(request: Request):
  form = await request.form()
  try:
    if field(form, "action") == "create":
      api(
        "/host-volumes",
        "POST",
        {
          "tag": field(form, "tag"),
          "path": field(form, "path"),
          "readonly": bool(form.get("readonly")),
          "require_mount": bool(form.get("require_mount")),
        },
      )
      return see(f"/volumes?ok=Added+host+volume+{quote(field(form, 'tag'))}")
    if field(form, "action") == "delete":
      api(f"/host-volumes/{quote(field(form, 'tag'))}", "DELETE")
      return see(f"/volumes?ok=Removed+host+volume+{quote(field(form, 'tag'))}")
    return see("/volumes")
  except ApiError as e:
    return see(f"/volumes?err={quote(str(e))}")


@app.post("/apps/{app_id}")
async def post_app(app_id: str, request: Request):
  """One action per submit: a lifecycle verb, or one kind of config change."""
  form = await request.form()
  app_id = unquote(app_id)
  here = f"/apps/{quote(app_id)}"
  action = field(form, "action")
  try:
    if action in ("start", "stop", "stage", "snapshot"):
      job = api("/jobs", "POST", {"verb": action, "args": {"app": app_id}})
      return see(f"{here}?job={quote(job['id'])}")
    if action == "cmd":
      job = api(
        "/jobs",
        "POST",
        {"verb": "cmd", "args": {"app": app_id, "command": field(form, "command")}},
      )
      return see(f"{here}?job={quote(job['id'])}")
    if action == "config":
      # Blank means "leave it alone" -- especially for secrets, whose
      # current value the UI never had in the first place.
      values = {
        key[len("set.") :]: str(form.get(key) or "")
        for key in form
        if key.startswith("set.") and str(form.get(key) or "").strip()
      }
      if not values:
        return see(f"{here}?ok=Nothing+to+change")
      api(f"{here}/config", "POST", {"set": values})
      return see(f"{here}?ok=Saved+{quote(str(len(values)))}+value(s)")
    if action == "bind":
      api(
        f"{here}/config", "POST", {"bind": {field(form, "volume"): field(form, "tag")}}
      )
      return see(f"{here}?ok=Bound+{quote(field(form, 'volume'))}")
    if action == "route":
      api(
        f"{here}/config", "POST", {"route": {field(form, "route"): field(form, "tag")}}
      )
      return see(f"{here}?ok=Assigned+{quote(field(form, 'route'))}")
    return see(here)
  except ApiError as e:
    return see(f"{here}?err={quote(str(e))}")


@app.post("/jobs")
async def proxy_job_submit(request: Request):
  """Forward a job from the command modal. JSON in, JSON out."""
  try:
    body = await request.json()
  except Exception:
    return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
  verb = body.get("verb") if isinstance(body, dict) else None
  args = body.get("args") if isinstance(body, dict) else None
  if not verb or not isinstance(args, dict):
    return JSONResponse({"error": "Expected verb and args"}, status_code=400)
  try:
    job = api("/jobs", "POST", {"verb": verb, "args": args})
  except ApiError as e:
    return JSONResponse({"error": str(e)}, status_code=400)
  return JSONResponse(job, status_code=202)


@app.get("/jobs/{job_id}")
def proxy_job(job_id: str):
  try:
    return api(f"/jobs/{quote(job_id)}")
  except ApiError as e:
    return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/activity/{filename}")
def proxy_activity_log(filename: str):
  try:
    return api(f"/activity/{quote(filename)}")
  except ApiError as e:
    return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/{path:path}")
def not_found(path: str):
  return html(
    f"/{path}".rstrip("/") or "/",
    "Not found",
    '<div class="card"><p class="empty">No such page.</p></div>',
    status_code=404,
  )
