"""
A small facade that caches the per-invocation harbordb and docker status.
"""

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from harbor.lib.appconfig import AppConfigStore, config_path
from harbor.lib.apps import AppID
from harbor.lib.config import Config
from harbor.lib.crypto import CryptoEngine
from harbor.lib.docker import HarborRunUnitStatus, load_harbor_run_unit_status
from harbor.lib.logtab import LogTab
from harbor.lib.observations import AppObservation, RunState, collect_observations
from harbor.lib.store import HarborDB

logger = logging.getLogger("harbor")

# Long enough to ride out another harbor finishing a normal command, short
# enough that a stale lockfile does not look like a hang.
LOCK_TIMEOUT = 5.0

# Where lock activity is recorded. Not an app id, so it cannot collide with the
# per-app `<app_id>/status` keys the same log carries.
LOCK_KEY = "harbor/lock"


def _resolve_app_query(candidates: list[AppID], query: str) -> list[AppID]:
  """Resolve a user-supplied app id (possibly ``app@version``) to a :class:`AppHandle`."""
  query, _, version = query.partition("@")

  if version:
    logger.warning("resolve_app_query: version %s not supported yet", version)

  # Attempt an exact match first
  found = [app for app in candidates if app == query]
  if found:
    return found

  # Attempt a match on the last segment of app_id
  return [app for app in candidates if app.stem == query]


class HarborCtx:
  def __init__(self, config: Config):
    self.config = config
    self._docker_status: dict[str, tuple[HarborRunUnitStatus, ...]] | None = None
    self._harbordb: HarborDB | None = None
    self._observations: dict[str, AppObservation] | None = None
    self._lock = FileLock(self.config.harbor_lockfile_path)

  @contextmanager
  def lock(self, by: str) -> Iterator[None]:
    """Hold the harbor lock for the duration of a command.
    Reentrant, so nesting is safe. All lib/ code assume as lock is being held, so
    basically grab this at context creation.
    """
    try:
      with self._lock.acquire(timeout=LOCK_TIMEOUT):
        try:
          self._record_lock("acquired", by)
          yield
        finally:
          self._record_lock("released", by)
    except Timeout:
      raise ValueError(
        f"Another process has locked harbor. Giving up after "
        f"{LOCK_TIMEOUT:g} seconds.\n"
        f"{self._lock_holder_hint()}"
        f"If no other harbor is running, remove {self.config.harbor_lockfile_path}"
      ) from None

  def _record_lock(self, state: str, by: str) -> None:
    LogTab(self.config.activity_log).write(
      LOCK_KEY,
      json.dumps(
        {
          "state": state,
          "by": by,
          "pid": os.getpid(),
          "at": LogTab.ts(),
        },
        separators=(",", ":"),
      ),
    )

  def _lock_holder_hint(self) -> str:
    """What the last lock record says, for a timed-out acquire."""
    raw = LogTab(self.config.activity_log).read(LOCK_KEY)
    if not raw:
      return ""
    try:
      record = json.loads(raw)
    except json.JSONDecodeError:
      return ""
    held = "Held" if record.get("state") == "acquired" else "Last held"
    return (
      f"{held} by `harbor {record.get('by', '?')}` "
      f"(pid {record.get('pid', '?')}, since {record.get('at', '?')}).\n"
    )

  def harbor_db(self) -> HarborDB:
    if self._harbordb is None:
      self._harbordb = HarborDB.from_config(self.config)
    return self._harbordb

  def run_path(self, app: AppID | str) -> Path:
    app_id = str(app)
    return self.config.run_root / app_id

  def app_path(self, app: AppID | str) -> Path:
    """The happ harbor is actually running: its own copy at ``run/<id>/happ``.

    Staging copies the bundle in, so what is installed is a fact on disk. It no
    longer depends on the catalog entry still existing, or still containing
    what it did at stage time.
    """
    happ = self.run_path(app) / "happ"
    if not (happ / "manifest.toml").is_file():
      raise ValueError(f"App {app} is not staged; run `harbor stage {app}` first")
    return happ

  def bundle_path(self, app: AppID | str) -> Path:
    """The catalog entry ``apps/<id>.happ`` -- the only thing `stage` copies from."""
    known = self.known_bundles().get(str(app))
    if known is None:
      raise ValueError(f'No app found for "{app}"')
    return known

  def is_staged(self, app: AppID | str) -> bool:
    return (self.run_path(app) / "happ" / "manifest.toml").is_file()

  def app_config(self, app: AppID | str) -> AppConfigStore:
    """The app's own config store under its run directory.

    Config lives with the app, so an app must be staged before it can be
    configured. `harbor start --set` covers the one-shot case.
    """
    app_id = str(app)
    run_path = self.run_path(app_id)
    if not run_path.is_dir():
      raise ValueError(f"App {app_id} is not staged; run `harbor stage {app_id}` first")
    return AppConfigStore(config_path(run_path), CryptoEngine.from_config(self.config))

  def known_bundles(self) -> dict[str, Path]:
    """Map app_id -> catalog entry under apps/.
    A directory is a "Harbor App Bundle" iff:
     - It ends in .happ
     - It has a manifest.toml file

    Entries may be real directories or symlinks; harbor does not distinguish.
    This does not attempt to parse or validate the manifest contents.
    """
    return {
      entry.stem: entry
      for entry in self.config.apps_root.glob("*.happ")
      if entry.is_dir() and (entry / "manifest.toml").is_file()
    }

  def staged_app_ids(self) -> set[str]:
    """Every app id with a happ copy under run/."""
    run_root = self.config.run_root
    if not run_root.is_dir():
      return set()
    return {
      entry.name
      for entry in run_root.iterdir()
      if (entry / "happ" / "manifest.toml").is_file()
    }

  def known_apps(self) -> list[AppID]:
    # Staged apps stay resolvable by id even with no catalog entry, so an app
    # whose `apps/` folder was deleted can still be stopped and removed.
    ids = dict.fromkeys(self.known_bundles())
    ids.update(dict.fromkeys(sorted(self.staged_app_ids())))
    return [AppID(app_id) for app_id in ids]

  def resolve_app(self, query_app_id: str) -> AppID:
    candidates = self.known_apps()
    found = _resolve_app_query(candidates, query_app_id)

    if len(found) > 1:
      raise ValueError(f'Multiple apps matched app_id "{query_app_id}"')

    elif len(found) == 0:
      raise ValueError(f'No app found for "{query_app_id}"')

    return found[0]

  def docker_container_statuses(self) -> dict[str, tuple[HarborRunUnitStatus, ...]]:
    if self._docker_status is None:
      self._docker_status = load_harbor_run_unit_status()
    return self._docker_status

  def _observed_app_ids(self) -> set[str]:
    """Collect every app_id that has left a trace: a bundle dir, a run folder, a
    container, or a harbordb entry.
    """
    ids = set(self.known_bundles())
    if self.config.run_root.is_dir():
      ids |= {path.name for path in self.config.run_root.iterdir() if path.is_dir()}
    ids |= set(self.docker_container_statuses())
    ids |= set(self.harbor_db().app_ids())
    return ids

  def _resolve_state_id(self, app_id: str) -> str:
    """Like ``resolve_app`` but over the wider set of ids that have run state,
    not just loadable bundles.
    """
    query, _, version = app_id.partition("@")
    if version:
      logger.warning("resolve_app_query: version %s not supported yet", version)
    ids = self._observed_app_ids()
    if query in ids:
      return query
    matches = sorted(i for i in ids if i.split(".")[-1] == query)
    if len(matches) > 1:
      raise ValueError(f'Multiple apps matched app_id "{app_id}"')
    if not matches:
      raise ValueError(f'No app state found for "{app_id}"')
    return matches[0]

  def run_state(self, app_id: AppID | str) -> RunState:
    """ "Light" run-state of an app"""
    resolved = AppID(self._resolve_state_id(str(app_id)))
    run_path = self.config.app_run_path(resolved)
    return RunState(
      app_id=resolved,
      run_path=run_path,
      run_dir_exists=run_path.is_dir(),
      compose_exists=(run_path / "compose.yml").is_file(),
      containers=self.docker_container_statuses().get(resolved, ()),
    )

  def observations(self) -> tuple[AppObservation, ...]:
    if self._observations is None:
      self._observations = collect_observations(self)
    return tuple(self._observations[key] for key in sorted(self._observations))
