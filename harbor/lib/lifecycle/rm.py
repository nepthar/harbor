from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle._common import (
  container_recovery_message,
  logger,
  managed_volume_dirs,
)
from harbor.lib.lifecycle.run import stop
from harbor.lib.lifecycle.stage import stage

# Deleting config while keeping data regenerates an app's secrets against a
# database initialised with the old ones, and it comes back as an app that no
# longer starts. PURGE takes both together, so nothing can end up mismatched.
RemovalMode = Literal["uninstall", "reset", "purge"]

# The installation. Data and config stay.
UNINSTALL: RemovalMode = "uninstall"
# The data, and the installation with it, then installed again from the bundle.
# `app`-kind volumes ship inside the bundle, so a reset picks up a changed happ.
RESET: RemovalMode = "reset"
# All three, and the routes and host ports with them.
PURGE: RemovalMode = "purge"

_ACTIONS: dict[str, str] = {
  UNINSTALL: "uninstalled",
  RESET: "reset",
  PURGE: "removed",
}


@dataclass(frozen=True)
class RemovalPlan:
  """What a removal will delete, and what it deliberately will not."""

  app_id: AppID
  mode: RemovalMode
  run_path: Path | None
  config_path: Path | None
  volume_paths: tuple[Path, ...]
  host_paths: tuple[Path, ...]
  stop_first: bool
  # Resolved while planning, so an app whose catalog entry is gone or ambiguous
  # fails with everything still on disk.
  restage_from: Path | None

  @property
  def purges(self) -> bool:
    return self.mode == PURGE


def removal_plan(app_id: AppID, ctx: HarborCtx, *, mode: RemovalMode) -> RemovalPlan:
  """Work out what a removal would destroy, without destroying it."""
  state = ctx.run_state(app_id)
  if state.containers and not state.compose_exists:
    raise ValueError(container_recovery_message(app_id, ctx))

  host_paths: tuple[Path, ...] = ()
  config_path = ctx.config.app_config_path(app_id)
  if config_path.is_file():
    binds = ctx.app_store(app_id).list_binds()
    host_paths = tuple(
      ctx.config.host_volumes[tag].path
      for tag in binds.values()
      if tag in ctx.config.host_volumes
    )

  volumes: tuple[Path, ...] = ()
  if mode in (RESET, PURGE):
    volumes = tuple(d for d in managed_volume_dirs(app_id, ctx) if d.is_dir())

  return RemovalPlan(
    app_id=app_id,
    mode=mode,
    run_path=state.run_path,
    config_path=config_path if mode == PURGE and config_path.is_file() else None,
    volume_paths=volumes,
    host_paths=host_paths,
    stop_first=state.compose_exists,
    restage_from=ctx.bundle_path(app_id) if mode == RESET else None,
  )


def rm(plan: RemovalPlan, ctx: HarborCtx) -> None:
  """Carry out `plan`, and for a reset install the app again afterwards."""
  app_id = plan.app_id
  if plan.stop_first:
    logger.info("Stopping %s", app_id)
    stop(app_id, ctx)

  if plan.run_path is not None and plan.run_path.exists():
    shutil.rmtree(plan.run_path)
    logger.info("removed run directory %s", plan.run_path)

  if plan.config_path is not None and plan.config_path.is_file():
    plan.config_path.unlink()
    logger.info("removed config %s", plan.config_path)

  for path in plan.volume_paths:
    if path.is_dir():
      shutil.rmtree(path)
      logger.info("removed volume %s", path)

  if plan.purges:
    ctx.harbor_db.purge_app(app_id)

  if plan.restage_from is not None:
    # Staging recreates the volume directories the compose file binds to, which is
    # why a reset can delete them outright.
    logger.info("Staging %s from %s", app_id, plan.restage_from)
    stage(app_id, plan.restage_from, ctx)

  # The activity log outlives the app on purpose, so close it out rather than
  # leaving the trail ending at whatever happened before the removal.
  action = _ACTIONS[plan.mode]
  record_app_action(action, app_id, ctx)
  logger.info("%s %s", action, app_id)
