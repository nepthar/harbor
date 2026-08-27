from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.docker import DockerError, docker_run_command
from harbor.lib.happ import HAPP_SUFFIX
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle._common import logger
from harbor.lib.lifecycle.routes import (
  assigned_routes,
  preflight_app_routes,
  register_app_routes,
  unregister_app_routes,
)
from harbor.lib.lifecycle.run import recovery_lines
from harbor.lib.lifecycle.stage import link_host_volumes, unlink_host_volumes
from harbor.lib.routes import RouteProviderError
from harbor.lib.run_layout import AppRunData, load_run_data
from harbor.lib.stack import AppStack


@dataclass(frozen=True)
class DevPlan:
  """A validated foreground run of a staged app against its source bundle."""

  app_id: AppID
  run_path: Path
  source: Path
  # app volume name -> the path inside `source` it will be mounted from.
  mounts: dict[str, Path]
  stack: AppStack
  run_data: AppRunData
  # The compose.yml about to run was generated from a manifest that has since
  # changed.
  manifest_stale: bool
  # route name -> public URL, for the routes this run will publish. Empty
  # unless `--routes` asked for it; a dev run is unpublished by default.
  published: dict[str, str]


def _dev_source(app: AppID, ctx: HarborCtx) -> Path:
  """The `.happ` folder the staged app came from."""
  origin = ctx.staged_origin(app)
  if origin is None:
    raise ValueError(
      f"App {app} has no recorded origin; reinstall it with `harbor install {app}`"
    )
  if (
    not origin.name.endswith(HAPP_SUFFIX)
    or not origin.is_dir()
    or not (origin / "manifest.toml").is_file()
  ):
    raise ValueError(
      f"App {app} was staged from {origin}, which is not a {HAPP_SUFFIX} folder. "
      f"`harbor dev` mounts the source in place, so it needs a directory bundle."
    )
  return origin.resolve()


def dev_plan(app: AppID, ctx: HarborCtx, *, publish_routes: bool = False) -> DevPlan:
  """Work out what a dev run would mount, and refuse if it cannot run at all."""
  paths = ctx.staged_paths(app)
  if not paths.exists() or not paths.compose_path.is_file():
    raise ValueError(f"App {app} is not installed; run `harbor install {app}` first")

  running = ctx.run_state(app).running_count
  if running:
    raise ValueError(
      f"App {app} has {running} running Harbor-labeled container(s); "
      f"run `harbor stop {app}` first"
    )

  # Required whether or not anything is mounted from it: `harbor dev` is for
  # an app you are editing in place, and that is what a folder bundle is.
  source = _dev_source(app, ctx)
  stack = AppStack.from_file(paths.manifest_path, app)

  mounts = {
    name: source / (volume.src or name)
    for name, volume in stack.volumes.items()
    if volume.kind == "app"
  }
  for name, path in mounts.items():
    if not path.exists():
      raise ValueError(f"App {app} - volume {name}: {path} does not exist")

  # Same bar as `start`: a dev run is a normal run with the happ mounted from
  # somewhere else, so unset config, binds and routes block it identically.
  run_data = load_run_data(stack, ctx)
  if run_data.start_blockers:
    raise ValueError("\n".join(recovery_lines(app, run_data.start_blockers)))

  published: dict[str, str] = {}
  if publish_routes:
    published = {
      name: run_data.route_urls[name]
      for name, _, _ in assigned_routes(run_data, ctx)
      if name in run_data.route_urls
    }

  return DevPlan(
    app_id=app,
    run_path=paths.run_path,
    source=source,
    mounts=mounts,
    stack=stack,
    run_data=run_data,
    manifest_stale=ctx.manifest_stale(app),
    published=published,
  )


@contextmanager
def source_volume_links(plan: DevPlan) -> Iterator[None]:
  """Point `volumes/app/*` at the source bundle, then put them back."""
  saved: list[tuple[Path, Path, Path]] = []
  for name, target in plan.mounts.items():
    link = plan.run_path / plan.stack.volumes[name].run_rel_path
    if not link.is_symlink():
      raise ValueError(
        f"App {plan.app_id} - volume {name}: {link} is not a link; "
        f"reinstall with `harbor install {plan.app_id}`"
      )
    saved.append((link, link.readlink(), target))

  try:
    for link, _, target in saved:
      link.unlink()
      link.symlink_to(target)
      logger.debug("dev volume %s -> %s", link, target)
    yield
  finally:
    for link, original, _ in saved:
      link.unlink(missing_ok=True)
      link.symlink_to(original)


def _unpublish(app: AppID, ctx: HarborCtx) -> None:
  """Best effort, like `stop`: a provider that will not answer must not keep
  the containers up or the links borrowed."""
  try:
    unregister_app_routes(app, ctx)
  except Exception as e:
    logger.error("failed to unregister routes for %s: %s", app, e)


def _compose_down(plan: DevPlan) -> None:
  """Best effort: the links go back whether or not teardown succeeds."""
  try:
    docker_run_command(
      ["compose", "down"],
      cwd=plan.run_path,
      json_output=False,
      check=True,
      env=plan.run_data.config_env(),
    )
  except DockerError as e:
    logger.error("dev: leaving containers behind, `compose down` failed: %s", e)


def dev(plan: DevPlan, ctx: HarborCtx) -> int:
  """Run the stack in this terminal with its happ mounted from source."""
  app = plan.app_id
  if plan.published:
    try:
      preflight_app_routes(plan.run_data, ctx)
    except RouteProviderError as e:
      raise ValueError(str(e)) from e

  link_host_volumes(plan.stack, plan.run_data)
  try:
    with source_volume_links(plan):
      record_app_action("dev", app, ctx)
      if plan.published:
        try:
          register_app_routes(plan.run_data, ctx)
        except RouteProviderError as e:
          raise ValueError(str(e)) from e
      try:
        result = docker_run_command(
          ["compose", "up"],
          cwd=plan.run_path,
          json_output=False,
          check=False,
          env=plan.run_data.config_env(),
        )
      finally:
        if plan.published:
          _unpublish(app, ctx)
        _compose_down(plan)
  finally:
    unlink_host_volumes(plan.run_path)

  record_app_action("dev-stopped", app, ctx)
  return result.returncode
