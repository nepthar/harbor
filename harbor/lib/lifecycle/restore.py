from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle.stage import materialize
from harbor.lib.run_layout import AppRunData
from harbor.lib.stack import app_stack
from harbor.lib.util import validate_identifier

logger = getLogger("harbor.lifecycle.restore")

# Scratch names used while swapping in the run dir from a snapshot. Siblings of
# the run dir so the swap is a rename on one filesystem rather than a second
# copy -- same reason `stage` keeps its scratch under run/ (see stage.py).
INCOMING = ".restore.incoming"
OUTGOING = ".restore.outgoing"


@dataclass(frozen=True)
class RestorePlan:
  """What `harbor restore` will overwrite, and with what."""

  app_id: AppID
  snapshot_path: Path
  app_version: str
  run_path: Path
  # (path inside the snapshot, path it is copied back over)
  data_volumes: tuple[tuple[Path, Path], ...]


def snapshot_names(app: AppID, ctx: HarborCtx) -> list[str]:
  app_snapshots = ctx.config.snapshot_root / app
  if not app_snapshots.is_dir():
    return []
  return sorted(entry.name for entry in app_snapshots.iterdir() if entry.is_dir())


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
  validate_identifier(snapshot_name)

  snapshot_path = ctx.config.snapshot_root / app / snapshot_name
  if not snapshot_path.is_dir():
    available = snapshot_names(app, ctx)
    detail = "\n".join(f"  {name}" for name in available) if available else "  (none)"
    raise ValueError(f"No snapshot {snapshot_name} for {app}. Available:\n{detail}")

  manifest_path = snapshot_path / "happ" / "manifest.toml"
  for file in (
    snapshot_path / "snapshot.toml",
    snapshot_path / "config.logtab",
    manifest_path,
  ):
    if not file.is_file():
      raise ValueError(
        f"Snapshot {snapshot_path} is missing required file: {file}. "
        "It is incomplete and cannot be restored"
      )

  with open(snapshot_path / "snapshot.toml", "rb") as f:
    meta = tomllib.load(f)

  # A snapshot restored onto a different app would write one app's secrets and
  # data under another's id, so treat a mismatch as a wrong argument.
  if meta.get("app_id") != str(app):
    raise ValueError(
      f"Snapshot {snapshot_path} belongs to {meta.get('app_id')!r}, not {app}"
    )

  data_root = ctx.config.volume_roots["data"] / app
  data_volumes = []
  for name in meta.get("included_volumes", []):
    source = snapshot_path / "volumes" / "data" / name
    if not source.is_dir():
      raise ValueError(
        f"Snapshot {snapshot_path} lists data volume {name} but has no "
        f"contents for it at {source}"
      )
    data_volumes.append((source, data_root / name))

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

  return RestorePlan(
    app_id=app,
    snapshot_path=snapshot_path,
    app_version=str(meta.get("app_version", "")),
    run_path=ctx.staged_app_paths(app).run_path,
    data_volumes=tuple(data_volumes),
  )


def _scratch_paths(plan: RestorePlan, ctx: HarborCtx) -> tuple[Path, Path]:
  run_root = ctx.config.run_root
  return run_root / f".{plan.app_id}{INCOMING}", run_root / f".{plan.app_id}{OUTGOING}"


def _stage_incoming(plan: RestorePlan, ctx: HarborCtx) -> Path:
  """Copy the snapshot's run-dir half into scratch space (not yet live).

  This is the whole of it: a snapshot holds the happ and the config, and
  `materialize` generates the rest. Snapshots taken before compose.yml was
  dropped from the format still carry one; it is ignored.
  """
  incoming, outgoing = _scratch_paths(plan, ctx)
  for scratch in (incoming, outgoing):
    if scratch.exists():
      shutil.rmtree(scratch)

  incoming.mkdir(parents=True, mode=0o700)
  shutil.copytree(plan.snapshot_path / "happ", incoming / "happ")
  shutil.copy2(plan.snapshot_path / "config.logtab", incoming / "config.logtab")
  return incoming


def _commit_incoming(plan: RestorePlan, ctx: HarborCtx) -> None:
  """Swap the validated copy in, whatever was there before."""
  incoming, outgoing = _scratch_paths(plan, ctx)
  if plan.run_path.exists():
    os.replace(plan.run_path, outgoing)
  os.replace(incoming, plan.run_path)
  if outgoing.exists():
    shutil.rmtree(outgoing)


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


def restore(plan: RestorePlan, ctx: HarborCtx) -> AppRunData:
  """Replace an app's run dir and data volumes with a snapshot's.

  Roll-forward only: the restored state becomes the current state. Nothing
  records where it came from and nothing can be rolled back to.
  """
  app = plan.app_id
  incoming = _stage_incoming(plan, ctx)

  try:
    stack = app_stack(incoming / "happ", app)
    # Data first. It is the step that can be refused at a password prompt, and
    # failing here leaves the run dir exactly as it was.
    _restore_data_volumes(plan, ctx)
  except Exception:
    shutil.rmtree(incoming)
    raise

  _commit_incoming(plan, ctx)

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
