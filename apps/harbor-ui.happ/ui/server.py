"""HTTP front door: route a request to a page, or run a POST as a harbor verb."""

import time
from pathlib import Path
from urllib.parse import quote, unquote

import activity
import auth
import catalog
import dashboard
import installed
import snapshots
import volumes
from api import ApiError, api
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from layout import RATE_LIMITED, error_card, esc, limited_page, page, signin_page

# No OpenAPI: this app has no API consumers, and /docs would be one more
# surface behind the same one password.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

NO_STORE = {"Cache-Control": "no-store"}


# The harbord API this UI is written against. harbord bumps its own number
# when a response shape changes, so a mismatch means one of the two was
# installed without the other and fields this UI reads may be missing.
NEEDS_API = 17
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


GENERAL_HINT = (
  "Every request to this app shares one budget, so a browser left hammering it "
  "holds everyone up. Wait a moment, then reload."
)


RELOAD = "harbor reload harbor-ui"


def auth_hint(seconds):
  return (
    f"Ten sign-in attempts an hour, shared by everyone. Another opens in about "
    f"{max(1, round(seconds / 60))} minutes, or clear the count by restarting:"
  )


def wants_html(request):
  """A browser navigating or submitting a form, rather than the job modal's fetch.

  The method cannot tell these apart -- the sign-in form is a POST too -- and
  `fetch` asks for `*/*` where a browser names text/html.
  """
  return "text/html" in request.headers.get("accept", "")


def limited(request, bucket, hint, command=""):
  headers = {"Cache-Control": "no-store", "Retry-After": str(bucket.retry_after())}
  if not wants_html(request):
    return JSONResponse({"error": RATE_LIMITED}, 429, headers=headers)
  return HTMLResponse(limited_page(hint, command), status_code=429, headers=headers)


def safe_next(value):
  """A path on this server, or `/`. Keeps `?next=` from becoming a redirector."""
  return value if value.startswith("/") and not value.startswith("//") else "/"


class Bucket:
  """A fixed window: `limit` through it, then nothing until it turns over.

  The window is anchored to the first request after an idle gap, not to the
  wall clock, and a refusal does not move it -- so a flood costs the rest of
  the current window and nothing beyond it.
  """

  def __init__(self, limit, window):
    self.limit = limit
    self.window = window
    self.count = 0
    self.reset_at = 0.0

  def check(self):
    now = time.monotonic()
    if now - self.reset_at >= self.window:
      self.count = 0
      self.reset_at = now
    self.count += 1
    return self.count <= self.limit

  def retry_after(self):
    """Seconds left in the open window. Only meaningful just after a refusal."""
    return max(1, int(self.reset_at + self.window - time.monotonic()) + 1)


# One budget for the whole process, not one per client: behind a reverse proxy
# every request arrives from the proxy's address. `/static` is outside both --
# a refused stylesheet renders a page that looks broken rather than limited.
_general = Bucket(10, 1.0)
_auth = Bucket(10, 3600.0)


@app.middleware("http")
async def front_door(request: Request, call_next):
  """Rate limit, then session. Everything reaches a page through here."""
  path = request.url.path
  if path.startswith("/static/"):
    return await call_next(request)

  # Everything answers to the general bucket; a sign-in attempt answers to both,
  # so a flood spends itself on the per-second budget before it can eat far into
  # the hour's worth of guesses.
  if not _general.check():
    return limited(request, _general, GENERAL_HINT)
  if path == "/login" and request.method == "POST" and not _auth.check():
    return limited(request, _auth, auth_hint(_auth.retry_after()), RELOAD)

  if auth.is_open(path) or auth.valid(request.cookies.get(auth.COOKIE)):
    return await call_next(request)
  if not wants_html(request):
    return JSONResponse({"error": "Session expired. Reload and sign in."}, 401)
  # A form submission that lost its session cannot be replayed by landing on
  # it, and `next` would point at a path with no GET. Send those to the door.
  if request.method != "GET":
    return see("/login")
  here = path + (f"?{request.url.query}" if request.url.query else "")
  return see(f"/login?next={quote(here)}")


@app.get("/login")
def login_get(next: str = "/"):
  return HTMLResponse(signin_page(safe_next(next)), headers=NO_STORE)


@app.post("/login")
async def login_post(request: Request):
  form = await request.form()
  target = safe_next(field(form, "next"))
  if not auth.check_password(field(form, "password")):
    return HTMLResponse(
      signin_page(target, "That is not the admin password."),
      status_code=401,
      headers=NO_STORE,
    )
  value, max_age = auth.issue()
  response = see(target)
  # SameSite=Lax is what keeps another site from POSTing a harbor verb with
  # this cookie attached; there is no CSRF token anywhere in this app.
  response.set_cookie(
    auth.COOKIE,
    value,
    max_age=max_age,
    path="/",
    httponly=True,
    samesite="lax",
    secure=request.url.scheme == "https",
  )
  return response


@app.post("/logout")
def logout():
  response = see("/login")
  response.delete_cookie(auth.COOKIE, path="/")
  return response


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
  """The list moved onto the dashboard; old links land there rather than 404."""
  return see("/")


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


@app.get("/activity")
def activity_page():
  version, unreachable = harbor_version("/activity", "Activity")
  if unreachable:
    return unreachable
  try:
    body = activity.page()
  except ApiError as e:
    return html("/activity", "Activity", error_card(e), version)
  return html("/activity", "Activity", body, version)


@app.get("/logs")
def logs():
  """The page this used to live at; old links land on it rather than 404."""
  return see("/activity")


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
