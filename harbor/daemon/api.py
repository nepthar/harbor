"""The admin API's routes.

Reads do not take the harbor lock. They walk the filesystem the same way
`harbor ps` does, and the worst a concurrent write can do is show a listing
that was true a moment ago. Writes go through `JobRunner`, which does lock.

Every handler is a plain `def`: `harbor.lib` blocks, and FastAPI runs a
non-async endpoint in a threadpool, which is what a blocking call wants.

Errors have one shape, `{"error": "..."}`, whether they come from harbor, from
a route that does not exist, or from pydantic rejecting a body. A client that
has to branch on two error shapes will get one of them wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from harbor import VERSION
from harbor.daemon.jobs import JobRunner, validate
from harbor.lib import views
from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx

# Bumped when a response shape changes in a way a client would notice. The web
# UI ships separately from the daemon, so it has to be able to tell.
API_VERSION = 1

CtxFactory = Callable[[], HarborCtx]


class JobSubmission(BaseModel):
  """What `POST /jobs` accepts.

  Shape only. Whether the verb exists, whether its arguments are the ones it
  declares, and whether the app is installed are harbor questions, and
  `jobs.validate` answers them against a live context.
  """

  model_config = ConfigDict(extra="forbid")

  verb: str
  args: dict[str, str] = Field(default_factory=dict)


def _ctx(request: Request) -> HarborCtx:
  """A context per request. Harbor's state is the filesystem, and a request is
  the daemon's equivalent of an invocation -- nothing is carried between."""
  return request.app.state.ctx_factory()


def _runner(request: Request) -> JobRunner:
  return request.app.state.jobs


Ctx = Annotated[HarborCtx, Depends(_ctx)]
Jobs = Annotated[JobRunner, Depends(_runner)]


def create_app(ctx_factory: CtxFactory, jobs: JobRunner) -> FastAPI:
  app = FastAPI(
    title="Harbor admin API",
    version=f"{VERSION} (api {API_VERSION})",
    description="Read harbor state, and run harbor verbs as jobs.",
  )
  app.state.ctx_factory = ctx_factory
  app.state.jobs = jobs

  @app.exception_handler(StarletteHTTPException)
  def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

  @app.exception_handler(RequestValidationError)
  def _bad_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 400 rather than FastAPI's 422: a malformed body is a bad request, and one
    # status for "you sent something wrong" is easier to consume than two.
    problems = "; ".join(
      f"{'.'.join(str(p) for p in error['loc'] if p != 'body') or 'body'}: "
      f"{error['msg']}"
      for error in exc.errors()
    )
    return JSONResponse({"error": f"Invalid request body: {problems}"}, 400)

  @app.get("/", tags=["meta"])
  @app.get("/version", tags=["meta"])
  def get_version() -> dict:
    return {"harbor": VERSION, "api": API_VERSION}

  @app.get("/apps", tags=["apps"])
  def list_apps(ctx: Ctx) -> dict:
    return {"apps": views.apps_view(ctx)}

  @app.get("/apps/{app_id}", tags=["apps"])
  def get_app(app_id: str, ctx: Ctx) -> dict:
    try:
      resolved = AppID(app_id)
    except ValueError:
      raise HTTPException(404, f"No app {app_id!r}") from None
    try:
      return views.app_view(resolved, ctx)
    except (ValueError, RuntimeError) as e:
      raise HTTPException(404, str(e)) from e

  @app.get("/jobs", tags=["jobs"])
  def list_jobs(jobs: Jobs) -> dict:
    return {"jobs": jobs.list()}

  @app.post("/jobs", status_code=202, tags=["jobs"])
  def submit_job(submission: JobSubmission, ctx: Ctx, jobs: Jobs) -> dict:
    try:
      validate(submission.verb, submission.args, ctx)
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e
    return jobs.submit(submission.verb, submission.args)

  @app.get("/jobs/{job_id}", tags=["jobs"])
  def get_job(job_id: str, jobs: Jobs) -> dict:
    job = jobs.get(job_id)
    if job is None:
      raise HTTPException(404, f"No job {job_id!r}")
    return job

  return app
