from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harbor.lib.apps import AppID, read_app_actions
from harbor.lib.docker import HarborRunUnitStatus, load_harbor_run_unit_status

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


# Where an app stands, as one word.
INSTALLED = "installed"  # there is a run dir harbor can start from
UNINSTALLED = "uninstalled"  # no run dir, but harbor still holds state for it
AVAILABLE = "available"  # a catalog entry and nothing else


def app_state(
  *,
  run_dir_exists: bool,
  config_exists: bool,
  volumes_exist: bool,
  has_containers: bool = False,
) -> str:
  """`installed`, `uninstalled`, or `available`.

  `uninstalled` is the state `harbor uninstall` leaves behind: the
  installation is gone, but the data, config and address it kept are all
  still here, which is what makes reinstalling pick up where it left off.

  Container state is optional because asking docker costs a subprocess, and
  a caller building a catalog listing has no reason to pay for it -- an app
  with containers but no run dir is a broken state for `doctor`, not a
  distinction a listing needs to draw.
  """
  if run_dir_exists or has_containers:
    return INSTALLED
  # Config and volumes are what `uninstall` keeps, so they are what makes
  # this the uninstalled state rather than nothing. A lone harbordb row is
  # not: that is an orphan for `doctor` to report, not a kept app.
  if config_exists or volumes_exist:
    return UNINSTALLED
  return AVAILABLE


@dataclass(frozen=True)
class AppObservation:
  """A union of every possible place an app can leave a trace - for diagnostics and status"""

  app_id: AppID
  bundle_path: Path | None
  run_dir_exists: bool
  compose_exists: bool
  config_exists: bool
  volumes_exist: bool
  containers: tuple[HarborRunUnitStatus, ...]
  db_present: bool
  last_action: str | None

  @property
  def running_count(self) -> int:
    return sum(container.state.lower() == "running" for container in self.containers)

  @property
  def state(self) -> str:
    """Where this app stands. See `app_state`."""
    return app_state(
      run_dir_exists=self.run_dir_exists,
      config_exists=self.config_exists,
      volumes_exist=self.volumes_exist,
      has_containers=bool(self.containers),
    )

  @property
  def installed(self) -> bool:
    return self.state == INSTALLED

  @property
  def known(self) -> bool:
    """Whether this id is more than a catalog entry -- something harbor put
    on disk, in docker, or in its own db."""
    return self.state != AVAILABLE or self.db_present

  @property
  def status(self) -> str:
    """Container state as one word: what an operator scanning a list wants.

    Strictly about containers. Whether the app is installed at all is a
    separate axis -- see `state` -- and conflating them is what made a
    listing claim an uninstalled app was merely `stopped`.
    """
    if self.running_count:
      return "running"
    if self.containers:
      return "exited"
    return "stopped"

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
  docker = load_harbor_run_unit_status()
  db_ids = set(ctx.harbor_db.app_ids())
  config_ids = ctx.config.app_config_ids()
  app_ids = set(bundles) | run_ids | set(docker) | db_ids | config_ids

  actions = read_app_actions(ctx)

  observations: dict[str, AppObservation] = {}
  for raw_id in app_ids:
    app_id = AppID(raw_id)
    paths = ctx.staged_paths(app_id)
    action = actions.get(raw_id)
    observations[app_id] = AppObservation(
      app_id=app_id,
      bundle_path=bundles.get(raw_id),
      run_dir_exists=paths.run_path.is_dir(),
      compose_exists=paths.compose_path.is_file(),
      config_exists=raw_id in config_ids,
      # Spelled out rather than borrowing `lifecycle.managed_volume_dirs`,
      # which would import back through HarborCtx into this module.
      volumes_exist=any(
        (root / raw_id).is_dir() for root in ctx.config.volume_roots.values()
      ),
      containers=docker.get(raw_id, ()),
      db_present=raw_id in db_ids,
      last_action=action[1] if action else None,
    )
  return observations
