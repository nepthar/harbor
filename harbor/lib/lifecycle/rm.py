from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle._common import (
  container_recovery_message,
  logger,
  managed_volume_dirs,
)
from harbor.lib.lifecycle.run import stop


@dataclass(frozen=True)
class RemovalPlan:
  """What `harbor rm` will delete, and what it deliberately will not."""

  app_id: AppID
  run_path: Path
  config_path: Path
  volume_paths: tuple[Path, ...]
  host_paths: tuple[Path, ...]
  stop_first: bool


def removal_plan(app_id: AppID, ctx: HarborCtx) -> RemovalPlan:
  """Work out what removing an app would destroy, without destroying it."""
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

  return RemovalPlan(
    app_id=app_id,
    run_path=state.run_path,
    config_path=config_path,
    volume_paths=tuple(d for d in managed_volume_dirs(app_id, ctx) if d.is_dir()),
    host_paths=host_paths,
    stop_first=state.compose_exists,
  )


def rm(plan: RemovalPlan, ctx: HarborCtx) -> None:
  """Stop an app and delete its run dir, config, managed volumes and routes.

  The catalog entry and any host-volume contents survive, so
  `harbor rm foo; harbor start foo` is a clean reinstall.

  TODO(docs/run-layout.md §8): capture a snapshot and verify its checksum
  before the first byte is deleted, once `harbor snapshot` exists. Until then
  this is unrecoverable and the CLI says so.
  """
  app_id = plan.app_id
  if plan.stop_first:
    logger.info("Stopping %s", app_id)
    stop(app_id, ctx)

  if plan.run_path.exists():
    shutil.rmtree(plan.run_path)
    logger.info("removed run directory %s", plan.run_path)

  if plan.config_path.is_file():
    plan.config_path.unlink()
    logger.info("removed config %s", plan.config_path)

  for path in plan.volume_paths:
    if path.is_dir():
      shutil.rmtree(path)
      logger.info("removed volume %s", path)

  ctx.harbor_db.purge_app(app_id)

  # The activity log outlives the app on purpose, so close it out rather than
  # leaving the trail ending at whatever happened before the removal.
  record_app_action("removed", app_id, ctx)
  logger.info("removed %s", app_id)
