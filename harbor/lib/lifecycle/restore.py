from __future__ import annotations

import shlex
import shutil
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle.snapshot import (
  SNAPSHOT_TAR_SUFFIX,
  extract_snapshot,
  remove_snapshot_dir,
  snapshot,
  snapshot_archive,
)
from harbor.lib.lifecycle.stage import materialize
from harbor.lib.run_layout import AppRunData
from harbor.lib.stack import AppStack
from harbor.lib.util import validate_identifier

logger = getLogger("harbor.lifecycle.restore")

# Label of the automatic safety snapshot taken before a restore overwrites the
# live state. Snapshot archives are named <timestamp>_<label>.tar.gz.
PRE_RESTORE_LABEL = "pre-restore"


@dataclass(frozen=True)
class RestorePlan:
  """What `harbor restore` will overwrite, and with what."""

  app_id: AppID
  snapshot_path: Path
  app_version: str
  run_path: Path
  config_path: Path
  # (path inside the snapshot, path it is copied back over)
  data_volumes: tuple[tuple[Path, Path], ...]
  # True when the target is the app's newest pre-restore snapshot, i.e. this
  # restore is undoing the last one. No new pre-restore snapshot is taken then,
  # or every undo would mint another snapshot and chase its own tail.
  is_latest_pre_restore: bool = False


def snapshot_names(app: AppID, ctx: HarborCtx) -> list[str]:
  app_snapshots = ctx.config.snapshot_root / app
  if not app_snapshots.is_dir():
    return []
  names = []
  for entry in app_snapshots.iterdir():
    if entry.is_file() and entry.name.endswith(SNAPSHOT_TAR_SUFFIX):
      names.append(entry.name.removesuffix(SNAPSHOT_TAR_SUFFIX))
  return sorted(names)


def snapshotted_app_ids(ctx: HarborCtx) -> list[AppID]:
  root = ctx.config.snapshot_root
  if not root.is_dir():
    return []
  found = []
  for entry in sorted(root.iterdir()):
    if not entry.is_dir():
      continue
    try:
      found.append(AppID(entry.name))
    except ValueError:
      continue
  return found


def resolve_snapshot_app(ctx: HarborCtx, query: str) -> AppID:
  """Resolve an app id against snapshots/, not the catalog or run/.

  Restoring an app that was removed outright is the whole point, so the id has
  to stay resolvable when nothing but its snapshots is left.
  """
  ids = snapshotted_app_ids(ctx)

  if query in ids:
    return AppID(query)

  matches = [app_id for app_id in ids if app_id.stem == query]
  if len(matches) > 1:
    raise ValueError(f'Multiple apps matched app_id "{query}"')
  if not matches:
    raise ValueError(
      f'No snapshots found for "{query}" under {ctx.config.snapshot_root}'
    )
  return matches[0]


def restore_plan(app: AppID, snapshot_name: str, ctx: HarborCtx) -> RestorePlan:
  """Work out what restoring would overwrite, without overwriting it."""
  snapshot_name = snapshot_name.removesuffix(SNAPSHOT_TAR_SUFFIX)
  validate_identifier(snapshot_name)

  snapshot_path = ctx.config.snapshot_root / app / snapshot_name
  archive = snapshot_archive(ctx.config.snapshot_root, app, snapshot_name)
  if not archive.is_file():
    available = snapshot_names(app, ctx)
    detail = "\n".join(f"  {name}" for name in available) if available else "  (none)"
    raise ValueError(f"No snapshot {snapshot_name} for {app}. Available:\n{detail}")

  prefix = f"{snapshot_name}/"
  with tarfile.open(archive, "r:gz") as tar:
    infos = {info.name.lstrip("./"): info for info in tar.getmembers()}
    for rel in (
      f"{prefix}snapshot.toml",
      f"{prefix}config.logtab",
      f"{prefix}happ/manifest.toml",
    ):
      if rel not in infos:
        raise ValueError(
          f"Snapshot {archive} is missing required file: {rel}. "
          "It is incomplete and cannot be restored"
        )
    meta_file = tar.extractfile(infos[f"{prefix}snapshot.toml"])
    if meta_file is None:
      raise ValueError(
        f"Snapshot {archive} is missing required file: {prefix}snapshot.toml. "
        "It is incomplete and cannot be restored"
      )
    meta = tomllib.load(meta_file)

  # A snapshot restored onto a different app would write one app's secrets and
  # data under another's id, so treat a mismatch as a wrong argument.
  if meta.get("app_id") != str(app):
    raise ValueError(f"Snapshot {archive} belongs to {meta.get('app_id')!r}, not {app}")

  data_root = ctx.config.volume_roots["data"] / app
  data_volumes = []
  for name in meta.get("included_volumes", []):
    vol_prefix = f"{prefix}volumes/data/{name}"
    if not any(m == vol_prefix or m.startswith(vol_prefix + "/") for m in infos):
      raise ValueError(
        f"Snapshot {archive} lists data volume {name} but has no "
        f"contents for it at {vol_prefix}"
      )
    data_volumes.append((snapshot_path / "volumes" / "data" / name, data_root / name))

  # The only current-state assumption restore keeps: clobbering files and data
  # volumes out from under live containers is how a restore becomes a corrupt
  # half-state.
  try:
    running_count = ctx.run_state(app).running_count
  except ValueError:
    running_count = 0
  if running_count:
    raise ValueError(
      f"App {app} has {running_count} running Harbor-labeled container(s); "
      f"run `harbor stop {app}` first"
    )

  pre_restores = [
    name for name in snapshot_names(app, ctx) if name.endswith(f"_{PRE_RESTORE_LABEL}")
  ]

  return RestorePlan(
    app_id=app,
    snapshot_path=snapshot_path,
    app_version=str(meta.get("app_version", "")),
    run_path=ctx.staged_paths(app).run_path,
    config_path=ctx.config.app_config_path(app),
    data_volumes=tuple(data_volumes),
    is_latest_pre_restore=bool(pre_restores) and snapshot_name == pre_restores[-1],
  )


def _rebuild_run_dir(plan: RestorePlan) -> None:
  """Drop the live run dir and rebuild it from the snapshot.

  A snapshot holds the happ and the config; `materialize` generates the rest.
  Snapshots taken before compose.yml was dropped from the format still carry
  one; it is ignored. Config is restored beside the run dir, not inside it.
  """
  if plan.run_path.exists():
    shutil.rmtree(plan.run_path)
  plan.run_path.mkdir(parents=True, mode=0o700)
  shutil.copytree(plan.snapshot_path / "happ", plan.run_path / "happ")
  plan.config_path.parent.mkdir(parents=True, exist_ok=True)
  shutil.copy2(plan.snapshot_path / "config.logtab", plan.config_path)


def _restore_data_volumes(plan: RestorePlan, ctx: HarborCtx) -> None:
  if not plan.data_volumes:
    return

  data_root = ctx.config.volume_roots["data"] / plan.app_id
  data_root.mkdir(parents=True, exist_ok=True)

  # Containers write as root, so both the removal and the copy need sudo. One
  # `sh -c` keeps it to a single password prompt, and means a refused password
  # fails before anything has been deleted. -a: recursive, keep
  # ownership/mode/times, preserve inner symlinks and hardlinks rather than
  # dereferencing them.
  targets = " ".join(shlex.quote(str(dest)) for _, dest in plan.data_volumes)
  sources = " ".join(shlex.quote(str(src)) for src, _ in plan.data_volumes)
  script = (
    f"set -e; rm -rf -- {targets}; cp -a -- {sources} {shlex.quote(str(data_root))}"
  )

  logger.warning("Asking for sudo access to copy data volumes out of the snapshot.")
  logger.warning("Command is: sudo sh -c %s", shlex.quote(script))
  result = subprocess.run(
    ["sudo", "sh", "-c", script],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    detail = (result.stderr or result.stdout or "").strip()
    message = (
      "Unable to restore data volumes — because docker containers often write "
      "files as root, sudo is required to replace volume contents. "
      "Ensure sudo is available and that you can authenticate when prompted."
    )
    raise RuntimeError(f"{message}\n{detail}" if detail else message)


def restore(
  plan: RestorePlan, ctx: HarborCtx, *, snapshot_first: bool = True
) -> AppRunData:
  """Replace an app's run dir, config, and data volumes with a snapshot's.

  When ``snapshot_first`` is true and a live run dir exists, a snapshot labeled
  ``pre-restore`` is taken first. If that fails, restore does not start.
  Restoring the newest pre-restore snapshot skips this — that restore is an
  undo, and snapshotting there would mint a new pre-restore on every undo.

  There is no scratch-and-swap: the pre-restore snapshot is the undo, so a
  failure partway leaves a broken run dir that another restore repairs.
  """
  sudo = bool(plan.data_volumes)
  archive = plan.snapshot_path.with_name(
    f"{plan.snapshot_path.name}{SNAPSHOT_TAR_SUFFIX}"
  )
  extract_snapshot(archive, sudo=sudo)

  try:
    return _restore_extracted(plan, ctx, snapshot_first=snapshot_first)
  finally:
    remove_snapshot_dir(plan.snapshot_path, sudo=sudo)


def _restore_extracted(
  plan: RestorePlan, ctx: HarborCtx, *, snapshot_first: bool
) -> AppRunData:
  app = plan.app_id

  # Parse the snapshot's happ before touching anything; a corrupt snapshot
  # fails here with the current state intact.
  stack = AppStack.from_file(plan.snapshot_path / "happ" / "manifest.toml", app)

  take_snapshot = snapshot_first and plan.run_path.exists()
  if take_snapshot and plan.is_latest_pre_restore:
    logger.info(
      "target is the newest %s snapshot; not taking another", PRE_RESTORE_LABEL
    )
    take_snapshot = False

  if take_snapshot:
    try:
      pre = snapshot(app, ctx, label=PRE_RESTORE_LABEL)
    except Exception as e:
      raise ValueError(
        f"Pre-restore snapshot of {app} failed; restore was not started. "
        f"Fix the problem below, or pass --no-snapshot to skip.\n{e}"
      ) from e
    logger.info("pre-restore snapshot written to %s", pre)

  # Volumes before the run dir: this is the step that can be refused at a sudo
  # password prompt, and refusal leaves the run dir untouched.
  _restore_data_volumes(plan, ctx)

  _rebuild_run_dir(plan)

  try:
    run_data, _ = materialize(stack, ctx)
  except Exception as e:
    # The run dir and volumes are already the snapshot's; only the generated
    # half is missing, which is what re-staging rebuilds.
    record_app_action("restore-failed", app, ctx.config)
    raise ValueError(
      f"App {app} was restored from {plan.snapshot_path}, but its compose.yml "
      f"and routes could not be rebuilt; fix the problem below and run "
      f"`harbor stage {app}`.\n{e}"
    ) from e

  record_app_action(f"restored - {plan.snapshot_path.name}", app, ctx.config)
  logger.info("restored %s from %s", app, plan.snapshot_path)
  return run_data
