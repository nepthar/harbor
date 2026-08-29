"""The admin API's routes.

Reads do not take the harbor lock; writes go through `JobRunner`, which does.
Every handler is a plain `def`: `harbor.lib` blocks, and FastAPI runs a
non-async endpoint in a threadpool. Errors have one shape, `{"error": "..."}`.
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
from harbor.jobs import JobRunner
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
from harbor.lib.lifecycle import apply_config_sets, bind
from harbor.lib.stack import AppStack

# Bumped when a response shape changes in a way a client would notice. The web
# UI ships separately from the daemon, so it has to be able to tell.
# 3: /activity endpoints; jobs carry a `log` path.
# 4: /snapshots; restore is a job.
# 5: jobs no longer carry `output`; read the file `log` names via /activity.
# 6: activity files are flat under var/logs; /activity/{filename}.
# 7: the `stage` verb is now `install`.
# 8: apps and catalog carry `state` (installed/uninstalled/available)
#    in place of the `staged` and `installed` booleans.
# 9: uninstall and reset are job verbs.
# 10: GET /metrics (gauge history).
# 11: /volumes bytes come from gauges; sizes=1 is gone; host volumes carry bytes.
# 12: restart is a job verb.
API_VERSION = 13

CtxFactory = Callable[[], HarborCtx]


class HostVolumeBody(BaseModel):
  """A `[host_volume]` entry, as the UI declares one."""

  model_config = ConfigDict(extra="forbid")

  path: str
  readonly: bool = False
  require_mount: bool = False


class NewHostVolume(HostVolumeBody):
  tag: str


class ConfigChange(BaseModel):
  """One `harbor config <app>` invocation, as the UI sends it."""

  model_config = ConfigDict(extra="forbid")

  set: dict[str, str] = Field(default_factory=dict)
  bind: dict[str, str] = Field(default_factory=dict)
  route: dict[str, str] = Field(default_factory=dict)


class JobSubmission(BaseModel):
  """What `POST /jobs` accepts. Shape only; `JobRunner.submit` validates."""

  model_config = ConfigDict(extra="forbid")

  verb: str
  args: dict[str, str] = Field(default_factory=dict)


def _ctx(request: Request) -> HarborCtx:
  """A context per request."""
  return request.app.state.ctx_factory()


def _runner(request: Request) -> JobRunner:
  return request.app.state.jobs


Ctx = Annotated[HarborCtx, Depends(_ctx)]
Jobs = Annotated[JobRunner, Depends(_runner)]


def _bundle_stack(app: AppID, ctx: HarborCtx) -> AppStack:
  """The schema for an app that is not installed yet."""
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
  # Recorded only. `start` registers assigned routes with their provider, so
  # a change lands the next time the app starts, like any other config value.
  ctx.app_store(app).set_route_assignment(route_name, tag)


def _ctx_again(ctx: HarborCtx) -> HarborCtx:
  """A context reading config.toml as it is *after* an edit."""
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
    """Set config values, host-volume binds and route assignments."""
    try:
      resolved = ctx.resolve_app(app_id)
      with ctx.locked(f"config {resolved}", resolved):
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
  def list_volumes(ctx: Ctx) -> dict:
    """Harbor-managed volumes, with sizes from the last volume-metrics run."""
    return {"volumes": views.volumes_view(ctx), **views.harbor_dir_sizes(ctx)}

  @app.get("/host-volumes", tags=["host volumes"])
  def list_host_volumes(ctx: Ctx) -> dict:
    return {"host_volumes": views.host_volumes_view(ctx)}

  @app.get("/snapshots", tags=["snapshots"])
  def list_snapshots(ctx: Ctx) -> dict:
    return {"snapshots": views.snapshots_view(ctx)}

  @app.post("/host-volumes", status_code=201, tags=["host volumes"])
  def create_host_volume(body: NewHostVolume, ctx: Ctx) -> dict:
    try:
      with ctx.harbor_lock(f"host-volume add {body.tag}"):
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
      with ctx.harbor_lock(f"host-volume set {tag}"):
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
      with ctx.harbor_lock(f"host-volume rm {tag}"):
        remove_host_volume(ctx, tag)
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e
    return {"host_volumes": views.host_volumes_view(_ctx_again(ctx))}

  @app.get("/activity", tags=["activity"])
  def list_activity(ctx: Ctx, app: str | None = None, limit: int = 20) -> dict:
    """Recorded unattended runs."""
    try:
      return {"activity": views.activity_view(ctx, app=app, limit=limit)}
    except ValueError as e:
      raise HTTPException(400, str(e)) from e

  @app.get("/activity/{filename}", tags=["activity"])
  def get_activity_log(filename: str, ctx: Ctx) -> dict:
    """One run's output file. The name is a validated filename, not a path."""
    try:
      return views.activity_log_view(ctx, filename)
    except ValueError as e:
      raise HTTPException(404, str(e)) from e

  @app.get("/metrics", tags=["metrics"])
  def get_metrics(ctx: Ctx, prefix: str = "", hours: int = 1) -> dict:
    """Gauge history. `prefix` is matched after `gauge/`."""
    if hours < 1:
      raise HTTPException(400, "hours must be >= 1")
    try:
      return views.metrics_view(ctx, prefix, hours)
    except ValueError as e:
      raise HTTPException(400, str(e)) from e

  @app.get("/jobs", tags=["jobs"])
  def list_jobs(jobs: Jobs) -> dict:
    return {"jobs": jobs.list()}

  @app.post("/jobs", status_code=202, tags=["jobs"])
  def submit_job(submission: JobSubmission, ctx: Ctx, jobs: Jobs) -> dict:
    try:
      return jobs.submit(submission.verb, submission.args, ctx)
    except (ValueError, RuntimeError) as e:
      raise HTTPException(400, str(e)) from e

  @app.get("/jobs/{job_id}", tags=["jobs"])
  def get_job(job_id: str, jobs: Jobs) -> dict:
    job = jobs.get(job_id)
    if job is None:
      raise HTTPException(404, f"No job {job_id!r}")
    return job

  return app
