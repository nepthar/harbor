"""HTTP front door: route a request to a page, or run a POST as a harbor verb."""

from pathlib import Path
from urllib.parse import quote, unquote

import activity
import catalog
import dashboard
import installed
import snapshots
import volumes
from api import ApiError, api
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from layout import error_card, esc, page

app = FastAPI()

NO_STORE = {"Cache-Control": "no-store"}


# The harbord API this UI is written against. harbord bumps its own number
# when a response shape changes, so a mismatch means one of the two was
# installed without the other and fields this UI reads may be missing.
NEEDS_API = 16
_daemon_api = None


def html(path, title, body, version="", status_code=200, actions="", subtitle=""):
  return HTMLResponse(
    page(
      path, title, _skew_notice() + body, version, actions=actions, subtitle=subtitle
    ),
    status_code=status_code,
    headers=NO_STORE,
  )


def _skew_notice():
  if _daemon_api is None or _daemon_api == NEEDS_API:
    return ""
  return (
    f'<div class="error"><h2>Version mismatch</h2><p>This page was built for '
    f"harbor API {NEEDS_API}, but harbord speaks {_daemon_api}. Buttons and "
    f"status may be wrong until the two match.</p>"
    f"<p>Reinstall this app and start it again:<br>"
    f"<code>harbor install harbor-ui</code><br>"
    f"<code>harbor start harbor-ui</code></p></div>"
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
  global _daemon_api
  try:
    info = api("/version")
  except ApiError as e:
    return "", html(path, title, error_card(e))
  _daemon_api = info.get("api")
  return info.get("harbor", ""), None


def field(form, name):
  value = form.get(name)
  return "" if value is None else str(value).strip()


@app.get("/")
def dashboard_get():
  version, err = harbor_version("/", "Dashboard")
  if err:
    return err
  try:
    body = dashboard.page(version)
  except ApiError as e:
    return html("/", "Dashboard", error_card(e), version)
  return html("/", "Dashboard", body, version)


@app.get("/apps")
def apps_list():
  version, err = harbor_version("/apps", "Apps")
  if err:
    return err
  title, body, version = installed.list_page(version)
  return html("/apps", title, body, version)


@app.get("/snapshots")
def snapshots_get(ok: str | None = None, err: str | None = None):
  version, unreachable = harbor_version("/snapshots", "Snapshots")
  if unreachable:
    return unreachable
  try:
    body = snapshots.page(banner(ok, err))
  except ApiError as e:
    return html("/snapshots", "Snapshots", error_card(e), version)
  return html("/snapshots", "Snapshots", body, version)


@app.get("/apps/{app_id}")
def app_detail(app_id: str, ok: str | None = None, err: str | None = None):
  version, unreachable = harbor_version(f"/apps/{app_id}", "Apps")
  if unreachable:
    return unreachable
  title, body, version, actions = installed.detail_page(
    app_id, version, notice=banner(ok, err)
  )
  return html(f"/apps/{app_id}", title, body, version, actions=actions)


@app.get("/volumes")
def volumes_get(ok: str | None = None, err: str | None = None):
  version, unreachable = harbor_version("/volumes", "Volumes")
  if unreachable:
    return unreachable
  try:
    body = volumes.volumes_page(banner(ok, err))
  except ApiError as e:
    return html("/volumes", "Volumes", error_card(e), version)
  return html("/volumes", "Volumes", body, version)


@app.get("/catalog")
def catalog_get(app: str = "", ok: str | None = None, err: str | None = None):
  version, unreachable = harbor_version("/catalog", catalog.TITLE)
  if unreachable:
    return unreachable
  title, body, version, actions = catalog.page(version, banner(ok, err), app=app)
  return html(
    "/catalog", title, body, version, actions=actions, subtitle=catalog.SUBTITLE
  )


@app.get("/logs")
def logs():
  version, unreachable = harbor_version("/logs", "Activity")
  if unreachable:
    return unreachable
  try:
    body = activity.page()
  except ApiError as e:
    return html("/logs", "Activity", error_card(e), version)
  return html("/logs", "Activity", body, version)


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
  """One config change per submit; lifecycle verbs go through the job modal."""
  form = await request.form()
  app_id = unquote(app_id)
  here = f"/apps/{quote(app_id)}"
  action = field(form, "action")
  try:
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
  """Forward a job from the job modal. JSON in, JSON out."""
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


app.mount(
  "/static",
  StaticFiles(directory=Path(__file__).parent / "static"),
  name="static",
)


@app.get("/{path:path}")
def not_found(path: str):
  return html(
    f"/{path}".rstrip("/") or "/",
    "Not found",
    '<div class="card"><p class="empty">No such page.</p></div>',
    status_code=404,
  )
