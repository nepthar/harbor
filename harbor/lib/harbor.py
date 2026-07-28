"""
A small facade that caches the per-invocation harbordb and docker status.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from harbor.lib.apps import AppID
from harbor.lib.config import Config
from harbor.lib.docker import HarborRunUnitStatus, load_harbor_run_unit_status
from harbor.lib.observations import AppObservation, RunState, collect_observations
from harbor.lib.store import AppDB, HarborDB

logger = logging.getLogger("harbor")

# Long enough to ride out another harbor finishing a normal command, short
# enough that a stale lockfile does not look like a hang.
LOCK_TIMEOUT = 5.0


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
  def lock(self) -> Iterator[None]:
    """Hold the harbor lock for the duration of a command.

    Reentrant, so nesting is safe. Waiting is bounded: without a timeout a
    stale lockfile is indistinguishable from a hang, with no output to explain
    it.
    """
    try:
      with self._lock.acquire(timeout=LOCK_TIMEOUT):
        yield
    except Timeout:
      raise ValueError(
        f"Another process has locked harbor. Giving up after "
        f"{LOCK_TIMEOUT:g} seconds.\n"
        f"If no other harbor is running, remove {self.config.harbor_lockfile_path}"
      ) from None

  def harbor_db(self) -> HarborDB:
    if self._harbordb is None:
      self._harbordb = HarborDB.from_config(self.config)
    return self._harbordb

  def run_path(self, app: AppID | str) -> Path:
    app_id = str(app)
    return self.config.run_root / app_id

  def app_path(self, app: AppID | str) -> Path:
    """Resolve a materialized happ's bundle via its `run/<app_id>/source` link.

    Every up'd app records its origin as this symlink. If the app was never
    materialized there is no link and this is an error. A dangling link
    (source deleted or moved) is a hard error telling the operator to restore
    it or ``harbor rm --runtime``.
    """
    link = self.run_path(app) / "source"
    if not link.is_symlink():
      raise ValueError(f"App {app} is not installed; run `harbor up {app}` first")
    target = link.readlink()
    if not target.exists():
      raise ValueError(
        f"Source for {app} is gone, was previously at {target}. "
        f"Restore the source or run `harbor rm --runtime {app}`."
      )
    return target

  def bundle_path(self, app: AppID | str) -> Path:
    """Resolve a loadable happ bundle without requiring materialization.

    Prefers the recorded ``run/<id>/source`` link when present and valid,
    otherwise the apps_root catalog entry. Raises if neither yields a
    ``*.happ`` directory with ``manifest.toml``.
    """
    app_id = str(app)
    staged = self._staged_sources().get(app_id)
    if staged is not None and not staged.exists():
      raise ValueError(
        f"Source for {app_id} is gone, was previously at {staged}. "
        f"Restore the source or run `harbor rm --runtime {app_id}`."
      )

    known = self.known_bundles().get(app_id)
    if known is not None:
      return known.resolve()
    raise ValueError(f'No app found for "{app_id}"')

  def app_db(self, app_id: AppID) -> AppDB:
    return self.harbor_db().app_db(app_id)

  def _staged_sources(self) -> dict[str, Path]:
    """Map app_id -> recorded source path for every staged happ.

    Reads the `run/<app_id>/source` links that staging writes for every app.
    Dangling links are included so the app still resolves (to the helpful error
    raised by ``app_path`` / ``bundle_path``).
    """
    run_root = self.config.run_root
    if not run_root.is_dir():
      return {}
    sources: dict[str, Path] = {}
    for entry in run_root.iterdir():
      link = entry / "source"
      if link.is_symlink():
        sources[entry.name] = link.readlink()
    return sources

  def known_bundles(self) -> dict[str, Path]:
    """Map app_id -> Harbor App bundle directory.
    A directory is a "Harbor App Bundle" iff:
     - It ends in .happ
     - It has a manifest.toml file

    This does not attempt to parse or validate the manifest contents.
    """
    bundles = {
      entry.stem: entry
      for entry in self.config.apps_root.glob("*.happ")
      if entry.is_dir() and (entry / "manifest.toml").is_file()
    }
    for app_id, source in self._staged_sources().items():
      if (
        source.exists()
        and source.suffix == ".happ"
        and source.is_dir()
        and (source / "manifest.toml").is_file()
      ):
        bundles[app_id] = source
      else:
        bundles.pop(app_id, None)
    return bundles

  def known_apps(self) -> list[AppID]:
    apps = {app_id: AppID(app_id) for app_id in self.known_bundles()}
    # Keep dangling installed apps resolvable by id (for unstage/down/etc.).
    for app_id in self._staged_sources():
      apps.setdefault(app_id, AppID(app_id))
    return list(apps.values())

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
