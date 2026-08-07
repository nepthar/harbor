from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harbor.lib.apps import AppID, read_app_actions
from harbor.lib.docker import HarborRunUnitStatus

if TYPE_CHECKING:
  from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor")


@dataclass(frozen=True)
class RunState:
  """The run-state of a single app, for lifecycle/snapshot operations."""

  app_id: AppID
  run_path: Path
  run_dir_exists: bool
  compose_exists: bool
  containers: tuple[HarborRunUnitStatus, ...]

  @property
  def running_count(self) -> int:
    return sum(container.state.lower() == "running" for container in self.containers)


@dataclass(frozen=True)
class AppObservation:
  """A union of every possible place an app can leave a trace - for diagnostics and status"""

  app_id: AppID
  bundle_path: Path | None
  run_dir_exists: bool
  compose_exists: bool
  containers: tuple[HarborRunUnitStatus, ...]
  db_present: bool
  last_action: str | None

  @property
  def running_count(self) -> int:
    return sum(container.state.lower() == "running" for container in self.containers)

  @property
  def run_display(self) -> str:
    if not self.run_dir_exists:
      return "missing"
    return "ready" if self.compose_exists else "broken"

  @property
  def app_display(self) -> str:
    return "ready" if self.bundle_path is not None else "missing"

  @property
  def container_display(self) -> str:
    if not self.containers:
      return "0"
    return f"{self.running_count}/{len(self.containers)} running"


def collect_observations(ctx: HarborCtx) -> dict[str, AppObservation]:
  bundles = ctx.known_bundles()
  run_ids = (
    {path.name for path in ctx.config.run_root.iterdir() if path.is_dir()}
    if ctx.config.run_root.is_dir()
    else set()
  )
  docker = ctx.docker_container_statuses()
  db_ids = set(ctx.harbor_db().app_ids())
  app_ids = set(bundles) | run_ids | set(docker) | db_ids

  actions = read_app_actions(ctx.config)

  observations: dict[str, AppObservation] = {}
  for raw_id in app_ids:
    app_id = AppID(raw_id)
    paths = ctx.staged_paths(app_id)
    last_action = actions.get(raw_id)
    observations[app_id] = AppObservation(
      app_id=app_id,
      bundle_path=bundles.get(raw_id),
      run_dir_exists=paths.run_path.is_dir(),
      compose_exists=paths.compose_path.is_file(),
      containers=docker.get(raw_id, ()),
      db_present=raw_id in db_ids,
      last_action=last_action,
    )
  return observations
