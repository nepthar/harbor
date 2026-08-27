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

# An app's state lives in three trees that come apart cleanly: the
# installation under `run/`, its data under the volume roots, and its config
# (with secrets, routes and host ports) under `config/`. A removal takes one
# of three combinations of them.
#
# These are combinations rather than three free switches on purpose. Deleting
# config while keeping data regenerates an app's secrets against a database
# initialised with the old ones, and it comes back as an app that no longer
# starts. PURGE takes both together, so nothing can end up mismatched.
RemovalMode = Literal["uninstall", "reset", "purge"]

# The installation. Data and config stay.
UNINSTALL: RemovalMode = "uninstall"
# The data, and the installation with it -- then staged again from the
# bundle. Config, secrets, routes and host ports stay, so the app comes back
# at the same address with the same settings and nothing in its volumes.
# Re-staging is what makes this useful while developing a happ: `app`-kind
# volumes are shipped inside the bundle, so a reset picks up whatever the
# happ says now rather than what it said when the app was first installed.
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
  """What a removal will delete, and what it deliberately will not.

  A `None` or empty field is a tree this removal keeps. `host_paths` is
  always kept -- it is carried so a confirmation can say so out loud.
  """

  app_id: AppID
  mode: RemovalMode
  run_path: Path | None
  config_path: Path | None
  volume_paths: tuple[Path, ...]
  host_paths: tuple[Path, ...]
  stop_first: bool
  # The bundle to stage again once the deleting is done, for a reset. It is
  # resolved while planning so an app whose catalog entry is gone or
  # ambiguous fails here, with everything still on disk, rather than after.
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
  """Carry out `plan`: stop the app, delete the trees it names, and -- for a
  reset -- stage it again from the bundle.

  The catalog entry always survives, and so do host-volume contents: a
  removal takes what harbor put under its own root, never the app itself or
  data the operator pointed it at.

  TODO(docs/run-layout.md §8): capture a snapshot and verify its checksum
  before the first byte is deleted. Until then this is unrecoverable and the
  CLI says so.
  """
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
    # Routes and host-port allocations were issued to the config that named
    # them, so they go together. An uninstall or a reset keeps the app's
    # address, which is the point of both.
    ctx.harbor_db.purge_app(app_id)

  if plan.restage_from is not None:
    # Staging rebuilds the run dir from the bundle and recreates the volume
    # directories the compose file binds to -- which is why a reset can
    # delete them outright, and why it picks up a happ that has changed.
    logger.info("Staging %s from %s", app_id, plan.restage_from)
    stage(app_id, plan.restage_from, ctx)

  # The activity log outlives the app on purpose, so close it out rather than
  # leaving the trail ending at whatever happened before the removal.
  action = _ACTIONS[plan.mode]
  record_app_action(action, app_id, ctx)
  logger.info("%s %s", action, app_id)
