import json
import logging
import os
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

from filelock import FileLock, Timeout

from harbor.lib.apps import AppID
from harbor.lib.config import Config
from harbor.lib.crypto import crypto_from_config
from harbor.lib.docker import load_harbor_run_unit_status
from harbor.lib.happ import load_happ, scan_happs
from harbor.lib.logtab import LogTab
from harbor.lib.observations import (
  AppObservation,
  RunState,
  app_state,
  collect_observations,
)
from harbor.lib.stack import AppStack
from harbor.lib.store import AppStore, HarborStore

logger = logging.getLogger("harbor")

# Long enough to ride out another harbor finishing a normal command, short
# enough that a stale lockfile does not look like a hang.
LOCK_TIMEOUT = 5.0

ACTIVITY_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10mb
ACTIVITY_LOG_HISTORY = 2000  # records

LIVE_METRIC_CUTOFF_AGE_SECONDS = 60 * 60 * 2  # 2 hours


def lock_timeout() -> float:
  """The acquire timeout, overridable via `HARBOR_LOCK_TIMEOUT`."""
  return float(os.environ.get("HARBOR_LOCK_TIMEOUT", LOCK_TIMEOUT))


def _lock_now() -> str:
  return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_lock_holder(path: Path, by: str) -> None:
  """Overwrite the lockfile with who holds it. Call after acquire; filelock
  truncates on acquire, so this is what a waiter reads on timeout."""
  path.write_text(
    json.dumps(
      {"by": by, "pid": os.getpid(), "at": _lock_now()},
      separators=(",", ":"),
    )
  )


def lock_holder_hint(path: Path) -> str:
  """What the lockfile says, for a timed-out acquire."""
  try:
    record = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError, ValueError):
    return ""
  return (
    f"Held by `harbor {record.get('by', '?')}` "
    f"(pid {record.get('pid', '?')}, since {record.get('at', '?')}).\n"
  )


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
class CatalogEntry:
  app_id: str
  path: Path
  source: str


def ambiguity_message(app: AppID | str, entries: tuple[CatalogEntry, ...]) -> str:
  locations = "\n".join(f"  {entry.source}: {entry.path}" for entry in entries)
  return (
    f'Multiple apps matched app_id "{app}":\n{locations}\n'
    f"Pass the full path to the one you mean, or remove the others."
  )


@dataclass(frozen=True)
class StagedAppPaths:
  app_id: AppID
  run_path: Path

  def exists(self) -> bool:
    return self.manifest_path.is_file()

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
    # FileLock will not create the parent. One FileLock per path so a nested
    # acquire in this process is reentrant rather than self-deadlocking.
    self.config.lock_root.mkdir(parents=True, exist_ok=True)
    self._harbor_lock = FileLock(self.config.harbor_lockfile_path)
    self._app_locks: dict[str, FileLock] = {}

    self.activity_log = LogTab(
      config.activity_log,
      auto_compact_size_bytes=ACTIVITY_LOG_MAX_BYTES,
      auto_compact_history=ACTIVITY_LOG_HISTORY,
    )

    self.metrics_log = LogTab(
      config.metrics_log,
    )

  def _app_filelock(self, app: AppID | str) -> FileLock:
    key = str(app)
    lock = self._app_locks.get(key)
    if lock is None:
      lock = FileLock(self.config.app_lockfile_path(key))
      self._app_locks[key] = lock
    return lock

  @contextmanager
  def _acquire(self, lock: FileLock, path: Path, by: str, what: str) -> Iterator[None]:
    timeout = lock_timeout()
    try:
      with lock.acquire(timeout=timeout):
        write_lock_holder(path, by)
        yield
    except Timeout:
      raise ValueError(
        f"Another process has locked {what}. Giving up after "
        f"{timeout:g} seconds.\n"
        f"{lock_holder_hint(path)}"
        f"If no other harbor is running, remove {path}"
      ) from None

  @contextmanager
  def harbor_lock(self, by: str) -> Iterator[None]:
    """Hold the harbor-wide lock. Reentrant. Library code assumes this is held
    whenever it mutates harbor-wide state (routes, harbordb, config.toml)."""
    with self._acquire(
      self._harbor_lock, self.config.harbor_lockfile_path, by, "harbor"
    ):
      yield

  @contextmanager
  def app_lock(self, app: AppID | str, by: str) -> Iterator[None]:
    """Hold one app's lock. Reentrant. Acquire before the harbor lock."""
    path = self.config.app_lockfile_path(app)
    with self._acquire(self._app_filelock(app), path, by, f"app {app}"):
      yield

  @contextmanager
  def locked(self, by: str, app: AppID | str | None = None) -> Iterator[None]:
    """App lock (if an app) then harbor lock. The usual lifecycle nest."""
    if app is None:
      with self.harbor_lock(by):
        yield
      return
    with self.app_lock(app, by):
      with self.harbor_lock(by):
        yield

  @cached_property
  def harbor_db(self) -> HarborStore:
    return HarborStore.from_config(self.config)

  def run_path(self, app: AppID | str) -> Path:
    app_id = str(app)
    return self.config.run_root / app_id

  def staged_paths(self, app: AppID | str) -> StagedAppPaths:
    app_id = AppID(app)
    return StagedAppPaths(app_id, self.config.app_run_path(app_id))

  def bundle_path(self, app: AppID | str) -> Path:
    """The catalog entry `stage` copies from. Exactly one, or an error."""
    entries = self.app_catalog().get(str(app), ())
    if len(entries) == 1:
      return entries[0].path
    elif not entries:
      raise ValueError(f'No app found for "{app}"')
    else:
      raise ValueError(ambiguity_message(app, entries))

  def is_staged(self, app: AppID | str) -> bool:
    return self.staged_paths(app).exists()

  def staged_stack(self, app: AppID | str) -> "AppStack | None":
    """The installed app's stack, or None when it is not installed.

    A manifest that no longer parses also reads as None, so one broken app cannot
    take a whole listing down.
    """
    paths = self.staged_paths(app)
    if not paths.manifest_path.is_file():
      return None
    try:
      return AppStack.from_file(paths.manifest_path, paths.app_id)
    except ValueError:
      return None

  def app_state(self, app: AppID | str) -> str:
    """Where one app stands, from the filesystem alone."""
    app_id = str(app)
    return app_state(
      run_dir_exists=self.staged_paths(app_id).run_path.is_dir(),
      config_exists=self.config.app_config_path(app_id).is_file(),
      volumes_exist=any(
        (root / app_id).is_dir() for root in self.config.volume_roots.values()
      ),
    )

  def bundle_stack(self, app: AppID | str) -> "AppStack | None":
    """The catalog bundle's stack, or None when there is no single bundle."""
    try:
      bundle = self.bundle_path(app)
    except ValueError:
      return None
    try:
      return load_happ(bundle).app_stack()
    except ValueError:
      return None

  def app_store(self, app: AppID | str) -> AppStore:
    """The app's own config store at ``config/<app_id>.logtab``."""
    return AppStore.from_path(
      self.config.app_config_path(app), crypto_from_config(self.config)
    )

  def app_catalog(self) -> dict[str, tuple[CatalogEntry, ...]]:
    """Every bundle in every app source, keyed by app id, in source order."""
    found: dict[str, list[CatalogEntry]] = {}
    for name, source_path in self.config.app_sources.items():
      for app_id, rel_path in scan_happs(source_path):
        found.setdefault(app_id, []).append(
          CatalogEntry(app_id, source_path / rel_path, name)
        )
    return {app_id: tuple(entries) for app_id, entries in found.items()}

  def staged_origin(self, app: AppID | str) -> Path | None:
    """The bundle an installed app was staged from, as `stage` recorded it."""
    if not self.config.app_config_path(app).is_file():
      return None
    origin = self.app_store(app).get_meta("origin")
    return Path(origin) if origin else None

  def manifest_stale(self, app: AppID | str) -> bool:
    """Whether the source bundle's manifest no longer matches the staged copy."""
    paths = self.staged_paths(app)
    origin = self.staged_origin(app)
    if origin is None or not paths.manifest_path.is_file():
      return False
    source = origin / "manifest.toml"
    if not source.is_file():
      return False
    return source.read_bytes() != paths.manifest_path.read_bytes()

  def known_bundles(self) -> dict[str, Path]:
    """Map app_id -> the one catalog entry harbor would use for it."""
    return {app_id: entries[0].path for app_id, entries in self.app_catalog().items()}

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

  def _observed_app_ids(self) -> set[str]:
    """Every app_id that has left a trace anywhere."""
    ids = set(self.known_bundles())
    if self.config.run_root.is_dir():
      ids |= {path.name for path in self.config.run_root.iterdir() if path.is_dir()}
    ids |= set(load_harbor_run_unit_status())
    ids |= set(self.harbor_db.app_ids())
    ids |= self.config.app_config_ids()
    return ids

  def _resolve_state_id(self, app_id: str) -> str:
    """Like `resolve_app`, but over every id with run state."""
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
    paths = self.staged_paths(resolved)
    return RunState(
      app_id=resolved,
      run_path=paths.run_path,
      run_dir_exists=paths.run_path.is_dir(),
      compose_exists=paths.compose_path.is_file(),
      containers=load_harbor_run_unit_status().get(resolved, ()),
    )

  def observations(self) -> tuple[AppObservation, ...]:
    collected = collect_observations(self)
    return tuple(collected[key] for key in sorted(collected))

  def record_gauge(self, name: str, reading: int | float) -> None:
    """Record a gauge reading at the current time."""
    key = "gauge/" + name
    self.metrics_log.write(key, str(reading))

  def read_gauges(self, prefix: str) -> dict[str, LogTab.Entry]:
    """Fetch the current values of all current gauges with the given prefix"""
    prefix = "gauge/" + prefix
    abs_cutoff = int(datetime.now(UTC).timestamp()) - LIVE_METRIC_CUTOFF_AGE_SECONDS
    all_found = self.metrics_log.scan(prefix)
    return {
      key: entry for key, entry in all_found.items() if entry.unix_seconds >= abs_cutoff
    }

  def history_gauges(self, prefix: str, days: int = 1) -> dict[str, list[LogTab.Entry]]:
    """Fetch the history of all gauges matching a given prefix for the last N days"""
    prefix = "gauge/" + prefix
    abs_cutoff = int(datetime.now(UTC).timestamp()) - days * 24 * 60 * 60
    all_found = self.metrics_log.history(prefix)
    by_key: dict[str, list[LogTab.Entry]] = defaultdict(list)
    for key, entry in all_found:
      if entry.unix_seconds >= abs_cutoff:
        by_key[key].append(entry)
    return dict(by_key)
