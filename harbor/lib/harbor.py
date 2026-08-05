"""
A small facade that caches the per-invocation harbordb and docker status.
"""

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

from harbor.lib.apps import AppID
from harbor.lib.config import Config
from harbor.lib.crypto import crypto_from_config
from harbor.lib.docker import HarborRunUnitStatus, load_harbor_run_unit_status
from harbor.lib.happ import scan_happs
from harbor.lib.logtab import LogTab
from harbor.lib.observations import AppObservation, RunState, collect_observations
from harbor.lib.store import AppStore, HarborStore

logger = logging.getLogger("harbor")

# Long enough to ride out another harbor finishing a normal command, short
# enough that a stale lockfile does not look like a hang.
LOCK_TIMEOUT = 5.0


def lock_timeout() -> float:
  """The acquire timeout, overridable via `HARBOR_LOCK_TIMEOUT`.

  Read per call rather than at import: the contention tests are the only thing
  that ever waits this out in full, and at 5s each they cost more than the rest
  of the suite combined.
  """
  return float(os.environ.get("HARBOR_LOCK_TIMEOUT", LOCK_TIMEOUT))


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


@dataclass(frozen=True)
class StagedAppPaths:
  app_id: AppID
  run_path: Path

  def exists(self) -> bool:
    return self.manifest_path.is_file()

  @property
  def config_path(self) -> Path:
    return self.run_path / "config.logtab"

  @property
  def compose_path(self) -> Path:
    return self.run_path / "compose.yml"

  @property
  def happ_path(self) -> Path:
    return self.run_path / "happ"

  @property
  def manifest_path(self) -> Path:
    return self.happ_path / "manifest.toml"


class HarborCtx:
  def __init__(self, config: Config):
    self.config = config
    self._docker_status: dict[str, tuple[HarborRunUnitStatus, ...]] | None = None
    self._harbordb: HarborStore | None = None
    self._observations: dict[str, AppObservation] | None = None
    self._lock = FileLock(self.config.harbor_lockfile_path)

  @contextmanager
  def lock(self, by: str) -> Iterator[None]:
    """Hold the harbor lock for the duration of a command.
    Reentrant, so nesting is safe. All lib/ code assume as lock is being held, so
    basically grab this at context creation.
    """
    timeout = lock_timeout()
    try:
      with self._lock.acquire(timeout=timeout):
        try:
          self._record_lock("acquired", by)
          yield
        finally:
          self._record_lock("released", by)
    except Timeout:
      raise ValueError(
        f"Another process has locked harbor. Giving up after "
        f"{timeout:g} seconds.\n"
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

  def harbor_db(self) -> HarborStore:
    if self._harbordb is None:
      self._harbordb = HarborStore.from_config(self.config)
    return self._harbordb

  def run_path(self, app: AppID | str) -> Path:
    app_id = str(app)
    return self.config.run_root / app_id

  def staged_app_paths(self, app: AppID | str) -> StagedAppPaths:
    app_id = AppID(app)
    return StagedAppPaths(app_id, self.config.app_run_path(app_id))

  def app_path(self, app: AppID | str) -> Path:
    """The happ harbor is actually running: its own copy at ``run/<id>/happ``.

    Staging copies the bundle in, so what is installed is a fact on disk. It no
    longer depends on the catalog entry still existing, or still containing
    what it did at stage time.
    """
    paths = self.staged_app_paths(app)
    if not paths.exists():
      raise ValueError(f"App {app} is not staged; run `harbor stage {app}` first")
    return paths.happ_path

  def bundle_path(self, app: AppID | str) -> Path:
    """The catalog entry ``apps/<id>.happ`` -- the only thing `stage` copies from."""
    known = self.known_bundles().get(str(app))
    if known is None:
      raise ValueError(f'No app found for "{app}"')
    return known

  def is_staged(self, app: AppID | str) -> bool:
    return self.staged_app_paths(app).exists()

  def app_store(self, app: AppID | str) -> AppStore:
    """The app's own config store under its run directory.

    Config lives with the app, so an app must be staged before it can be
    configured. `harbor start --set` covers the one-shot case.
    """
    paths = self.staged_app_paths(app)
    if not paths.run_path.is_dir():
      raise ValueError(f"App {app} is not staged; run `harbor stage {app}` first")
    return AppStore.from_path(paths.config_path, crypto_from_config(self.config))

  def known_bundles(self) -> dict[str, Path]:
    """Map app_id -> catalog entry under apps/.

    What counts as an entry is `happ.could_be_happ`'s call (via `scan_happs`);
    entries may be real directories/files or symlinks, and contents are not
    parsed or validated here. When an id has both flavors, the .happ
    directory wins (`scan_happs` yields it first).
    """
    apps_root = self.config.apps_root
    found: dict[str, Path] = {}
    for app_id, rel_path in scan_happs(apps_root):
      found.setdefault(app_id, apps_root / rel_path)
    return found

  def staged_app_ids(self) -> set[str]:
    """Every app id with a happ copy under run/."""
    run_root = self.config.run_root
    if not run_root.is_dir():
      return set()
    found: set[str] = set()
    for entry in run_root.iterdir():
      try:
        paths = StagedAppPaths(AppID(entry.name), entry)
      except ValueError:
        continue
      if paths.exists():
        found.add(entry.name)
    return found

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
    paths = self.staged_app_paths(resolved)
    return RunState(
      app_id=resolved,
      run_path=paths.run_path,
      run_dir_exists=paths.run_path.is_dir(),
      compose_exists=paths.compose_path.is_file(),
      containers=self.docker_container_statuses().get(resolved, ()),
    )

  def observations(self) -> tuple[AppObservation, ...]:
    if self._observations is None:
      self._observations = collect_observations(self)
    return tuple(self._observations[key] for key in sorted(self._observations))
