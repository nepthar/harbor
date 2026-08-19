"""The admin API's routes.

Reads do not take the harbor lock. They walk the filesystem the same way
`harbor ps` does, and the worst a concurrent write can do is show a listing
that was true a moment ago. Writes go through `JobRunner`, which does lock.

Handlers are sync on purpose: `harbor.lib` blocks, and starlette runs a
non-async endpoint in a threadpool, which is exactly the behaviour a blocking
call wants. The one exception is reading a request body, which is async.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from harbor import VERSION
from harbor.daemon.jobs import JobRunner, validate
from harbor.lib import views
from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx

# Bumped when a response shape changes in a way a client would notice. The web
# UI ships separately from the daemon, so it has to be able to tell.
API_VERSION = 1

CtxFactory = Callable[[], HarborCtx]


def _ctx(request: Request) -> HarborCtx:
  """A context per request. Harbor's state is the filesystem, and a request is
  the daemon's equivalent of an invocation -- nothing is carried between."""
  return request.app.state.ctx_factory()


def _jobs(request: Request) -> JobRunner:
  return request.app.state.jobs


def get_version(request: Request) -> JSONResponse:
  return JSONResponse({"harbor": VERSION, "api": API_VERSION})


def list_apps(request: Request) -> JSONResponse:
  return JSONResponse({"apps": views.apps_view(_ctx(request))})


def get_app(request: Request) -> JSONResponse:
  raw = request.path_params["app_id"]
  try:
    app_id = AppID(raw)
  except ValueError:
    raise HTTPException(404, f"No app {raw!r}") from None
  try:
    return JSONResponse(views.app_view(app_id, _ctx(request)))
  except (ValueError, RuntimeError) as e:
    raise HTTPException(404, str(e)) from e


def list_jobs(request: Request) -> JSONResponse:
  return JSONResponse({"jobs": _jobs(request).list()})


def get_job(request: Request) -> JSONResponse:
  job_id = request.path_params["job_id"]
  job = _jobs(request).get(job_id)
  if job is None:
    raise HTTPException(404, f"No job {job_id!r}")
  return JSONResponse(job)


async def submit_job(request: Request) -> JSONResponse:
  try:
    body = await request.json()
  except ValueError as e:
    raise HTTPException(400, f"Body is not valid JSON: {e}") from e
  return await run_in_threadpool(_submit, request, body)


def _submit(request: Request, body: Any) -> JSONResponse:
  if not isinstance(body, dict):
    raise HTTPException(400, 'Body must be a JSON object: {"verb": …, "args": {…}}')

  verb = body.get("verb")
  args = body.get("args", {})
  if not isinstance(verb, str):
    raise HTTPException(400, 'Body needs a "verb" string')
  if not isinstance(args, dict) or not all(
    isinstance(k, str) and isinstance(v, str) for k, v in args.items()
  ):
    raise HTTPException(400, '"args" must be an object of string values')

  try:
    validate(verb, args, _ctx(request))
  except (ValueError, RuntimeError) as e:
    raise HTTPException(400, str(e)) from e

  return JSONResponse(_jobs(request).submit(verb, args), status_code=202)


async def _render_error(request: Request, exc: HTTPException) -> JSONResponse:
  """One error shape for the whole API, including starlette's own 404s/405s."""
  return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


ROUTES = [
  Route("/", get_version),
  Route("/version", get_version),
  Route("/apps", list_apps),
  Route("/apps/{app_id}", get_app),
  Route("/jobs", list_jobs, methods=["GET"]),
  Route("/jobs", submit_job, methods=["POST"]),
  Route("/jobs/{job_id}", get_job),
]


def create_app(ctx_factory: CtxFactory, jobs: JobRunner) -> Starlette:
  app = Starlette(
    routes=ROUTES,
    exception_handlers={HTTPException: _render_error},
  )
  app.state.ctx_factory = ctx_factory
  app.state.jobs = jobs
  return app
