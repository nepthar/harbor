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
from harbor.lib.config import load_config_file
from harbor.lib.config_edit import (
  add_host_volume,
  remove_host_volume,
  set_host_volume,
)
from harbor.lib.happ import load_happ
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import apply_config_sets, bind, sync_route_assignment
from harbor.lib.routes import RouteProviderError
from harbor.lib.stack import AppStack

# Bumped when a response shape changes in a way a client would notice. The web
# UI ships separately from the daemon, so it has to be able to tell.
API_VERSION = 1

CtxFactory = Callable[[], HarborCtx]


class HostVolumeBody(BaseModel):
  """A `[host_volume]` entry, as the UI declares one.

  `path` is a host path, which is the one kind of argument the rest of this
  API refuses to take. That is deliberate here: declaring where apps may bind
  is the point of the endpoint, and it cannot be done without naming a path.
  """

  model_config = ConfigDict(extra="forbid")

  path: str
  readonly: bool = False
  require_mount: bool = False


class NewHostVolume(HostVolumeBody):
  tag: str


class ConfigChange(BaseModel):
  """One `harbor config <app>` invocation, as the UI sends it.

  Every value names something the manifest or config.toml already declares --
  a config key, a host_volume tag, a route provider tag. Nothing here takes a
  path, which is what keeps this endpoint inside the rule the rest of the API
  follows.
  """

  model_config = ConfigDict(extra="forbid")

  set: dict[str, str] = Field(default_factory=dict)
  bind: dict[str, str] = Field(default_factory=dict)
  route: dict[str, str] = Field(default_factory=dict)


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


def _bundle_stack(app: AppID, ctx: HarborCtx) -> AppStack:
  """The schema for an app that is not staged yet, so it can be configured
  before its first install -- the same fallback `harbor config` makes."""
  return load_happ(ctx.bundle_path(app)).app_stack()


def _assign_route(
  app: AppID, stack: AppStack, route_name: str, tag: str, ctx: HarborCtx
) -> None:
  if route_name not in stack.routes:
    known = ", ".join(sorted(stack.routes)) or "(none)"
    raise ValueError(
      f"route {route_name!r} is not declared in {app}'s manifest; known routes: {known}"
    )
  if tag not in ctx.config.route_providers:
    known = ", ".join(sorted(ctx.config.route_providers))
    raise ValueError(f"route provider {tag!r} is not configured; known tags: {known}")
  store = ctx.app_store(app)
  old_tag = store.get_route_assignment(route_name)
  store.set_route_assignment(route_name, tag)
  try:
    sync_route_assignment(app, route_name, old_tag, tag, ctx)
  except RouteProviderError as e:
    raise ValueError(str(e)) from e


def _ctx_again(ctx: HarborCtx) -> HarborCtx:
  """A context reading config.toml as it is *after* an edit.

  The request's own context loaded the file before the write, and every
  attribute on it is from that read.
  """
  return HarborCtx(load_config_file(ctx.config.config_path))


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

  @app.get("/catalog", tags=["catalog"])
  def list_catalog(ctx: Ctx) -> dict:
    return {"catalogs": views.catalog_view(ctx)}

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

  @app.post("/apps/{app_id}/config", tags=["apps"])
  def change_app_config(app_id: str, body: ConfigChange, ctx: Ctx) -> dict:
    """Set config values, host-volume binds and route assignments.

    Inline rather than as a job: the writes are local and quick. Assigning a
    route is the exception -- it calls out to the provider -- and is the first
    thing that would want moving if a slow proxy starts holding requests open.
    """
    try:
      resolved = ctx.resolve_app(app_id)
      stack = ctx.staged_stack(resolved) or _bundle_stack(resolved, ctx)
      if body.set:
        apply_config_sets(stack, list(body.set.items()), ctx)
      for volume_name, tag in body.bind.items():
        bind(stack, volume_name, tag, ctx)
      for route_name, tag in body.route.items():
        _assign_route(resolved, stack, route_name, tag, ctx)
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e
    return views.app_view(resolved, _ctx_again(ctx))

  @app.get("/volumes", tags=["volumes"])
  def list_volumes(ctx: Ctx, sizes: bool = False) -> dict:
    """Harbor-managed volumes. `sizes=1` walks every file, so it is opt-in."""
    return {"volumes": views.volumes_view(ctx, sizes=sizes)}

  @app.get("/host-volumes", tags=["host volumes"])
  def list_host_volumes(ctx: Ctx) -> dict:
    return {"host_volumes": views.host_volumes_view(ctx)}

  @app.post("/host-volumes", status_code=201, tags=["host volumes"])
  def create_host_volume(body: NewHostVolume, ctx: Ctx) -> dict:
    # Config edits are milliseconds, so they answer inline rather than as a
    # job. They still take the harbor lock, so one running behind a snapshot
    # waits and then fails naming the holder.
    try:
      add_host_volume(
        ctx,
        body.tag,
        body.path,
        readonly=body.readonly,
        require_mount=body.require_mount,
      )
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e
    return {"host_volumes": views.host_volumes_view(_ctx_again(ctx))}

  @app.put("/host-volumes/{tag}", tags=["host volumes"])
  def replace_host_volume(tag: str, body: HostVolumeBody, ctx: Ctx) -> dict:
    if tag not in ctx.config.host_volumes:
      raise HTTPException(404, f"No host volume {tag!r}")
    try:
      set_host_volume(
        ctx,
        tag,
        body.path,
        readonly=body.readonly,
        require_mount=body.require_mount,
      )
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e
    return {"host_volumes": views.host_volumes_view(_ctx_again(ctx))}

  @app.delete("/host-volumes/{tag}", tags=["host volumes"])
  def delete_host_volume(tag: str, ctx: Ctx) -> dict:
    if tag not in ctx.config.host_volumes:
      raise HTTPException(404, f"No host volume {tag!r}")
    try:
      remove_host_volume(ctx, tag)
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e
    return {"host_volumes": views.host_volumes_view(_ctx_again(ctx))}

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
