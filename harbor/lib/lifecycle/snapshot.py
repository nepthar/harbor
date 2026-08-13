from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from logging import getLogger
from pathlib import Path

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.stack import AppStack
from harbor.lib.util import validate_identifier

logger = getLogger("harbor.lifecycle.snapshot")

SNAPSHOT_TAR_SUFFIX = ".tar.gz"


def _toml_str_array(values: list[str]) -> str:
  return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _volume_names(volumes_root: Path) -> tuple[list[str], list[str]]:
  """Return (included data volume names, excluded other volume names)."""
  included: list[str] = []
  excluded: list[str] = []
  if not volumes_root.is_dir():
    return included, excluded
  for kind_dir in sorted(volumes_root.iterdir()):
    if not kind_dir.is_dir():
      continue
    names = sorted(p.name for p in kind_dir.iterdir())
    if kind_dir.name == "data":
      included.extend(names)
    else:
      excluded.extend(names)
  return included, excluded


def _staging_failure(staging: Path, message: str) -> RuntimeError:
  return RuntimeError(
    f"{message}\n"
    f"Incomplete snapshot left at {staging}; "
    f"remove it with `rm -rf {staging}` before retrying."
  )


def snapshot_archive(root: Path, app: AppID, name: str) -> Path:
  return root / app / f"{name}{SNAPSHOT_TAR_SUFFIX}"


def _tar_create(folder: Path, archive: Path, *, sudo: bool) -> None:
  cmd = ["tar", "-czf", "-", "-C", str(folder.parent), folder.name]
  if sudo:
    logger.warning("Asking for sudo access to compress the snapshot.")
    cmd = ["sudo", *cmd]
  with open(archive, "wb") as out:
    result = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE)
  if result.returncode != 0:
    archive.unlink(missing_ok=True)
    detail = (result.stderr or b"").decode().strip()
    message = f"Unable to compress snapshot at {folder}."
    if sudo:
      message = (
        "Unable to compress snapshot — sudo is required to read "
        "root-owned volume files in the snapshot. "
        "Ensure sudo is available and that you can authenticate when prompted."
      )
    raise RuntimeError(f"{message}\n{detail}" if detail else message)


def _tar_extract(archive: Path, dest_parent: Path, *, sudo: bool) -> None:
  cmd = ["tar", "-xzf", str(archive), "-C", str(dest_parent)]
  if sudo:
    logger.warning("Asking for sudo access to extract the snapshot.")
    cmd = ["sudo", *cmd]
  result = subprocess.run(cmd, capture_output=True, text=True)
  if result.returncode != 0:
    detail = (result.stderr or result.stdout or "").strip()
    message = f"Unable to extract snapshot {archive}."
    if sudo:
      message = (
        "Unable to extract snapshot — sudo is required to restore "
        "root-owned volume files. "
        "Ensure sudo is available and that you can authenticate when prompted."
      )
    raise RuntimeError(f"{message}\n{detail}" if detail else message)


def remove_snapshot_dir(folder: Path, *, sudo: bool) -> None:
  if not folder.exists():
    return
  if not sudo:
    shutil.rmtree(folder)
    return
  result = subprocess.run(
    ["sudo", "rm", "-rf", "--", str(folder)],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    detail = (result.stderr or result.stdout or "").strip()
    raise RuntimeError(
      f"Unable to remove {folder}; remove it with `sudo rm -rf {folder}`."
      + (f"\n{detail}" if detail else "")
    )


def extract_snapshot(archive: Path, *, sudo: bool) -> Path:
  name = archive.name.removesuffix(SNAPSHOT_TAR_SUFFIX)
  folder = archive.parent / name
  if folder.exists():
    raise ValueError(
      f"Incomplete snapshot extract left at {folder}; "
      f"remove it with `rm -rf {folder}` before retrying."
    )
  try:
    _tar_extract(archive, archive.parent, sudo=sudo)
  except Exception:
    remove_snapshot_dir(folder, sudo=sudo)
    raise
  return folder


def snapshot(
  app: AppID,
  ctx: HarborCtx,
  label: str = "",
) -> Path:
  paths = ctx.staged_paths(app)

  if not paths.run_path.exists():
    raise ValueError(f"App {app} is not staged and therefore cannot be snapshotted")

  try:
    running_count = ctx.run_state(app).running_count
  except ValueError:
    running_count = 0
  if running_count:
    raise ValueError(
      f"App {app} has {running_count} running Harbor-labeled container(s); "
      f"run `harbor stop {app}` first"
    )

  # Required files for the snapshot. If these don't exist, something is wrong
  # with the app.
  for file in (paths.manifest_path, paths.config_path):
    if not file.is_file():
      raise ValueError(
        f"App {app} missing required file: {file}. "
        "This app appears to be staged improperly"
      )

  folder_name = datetime.now(UTC).strftime("%Y-%m-%d_%H-%MZ")
  if label:
    validate_identifier(label)
    folder_name = f"{folder_name}_{label}"

  snapshot_folder = ctx.config.snapshot_root / app / folder_name
  archive = snapshot_archive(ctx.config.snapshot_root, app, folder_name)
  if archive.exists():
    raise ValueError(
      f"Snapshot already exists: {archive}. "
      "Are you taking multiple snapshots within the same minute?"
    )
  if snapshot_folder.exists():
    raise ValueError(
      f"Incomplete snapshot left at {snapshot_folder}; "
      f"remove it with `rm -rf {snapshot_folder}` before retrying."
    )

  staging = ctx.config.harbor_root / "temp" / "current_snapshot"
  if staging.exists():
    raise ValueError(
      f"Incomplete snapshot left at {staging}; "
      f"remove it with `rm -rf {staging}` before retrying."
    )

  staging.mkdir(parents=True, mode=0o700)
  included: list[str] = []

  try:
    included, excluded = _volume_names(paths.run_path / "volumes")
    app_version = AppStack.from_file(paths.manifest_path, app).version
    (staging / "snapshot.toml").write_text(
      "\n".join(
        [
          f'app_id = "{app}"',
          f'date = "{folder_name}"',
          f'app_version = "{app_version}"',
          f"included_volumes = {_toml_str_array(included)}",
          f"excluded_volumes = {_toml_str_array(excluded)}",
          "",
        ]
      ),
      encoding="utf-8",
    )

    # Config is harbor-owned; copy2 keeps mode and mtime. Secrets stay Fernet
    # ciphertext — we never decrypt on this path.
    #
    # compose.yml is not captured: its host ports are a photograph of harbordb,
    # which moves on. `restore` regenerates it from the happ below.
    shutil.copy2(paths.config_path, staging / "config.logtab")
    shutil.copytree(paths.happ_path, staging / "happ")

    data_vols = paths.run_path / "volumes" / "data"
    if data_vols.is_dir():
      data_dest = staging / "volumes" / "data"
      data_dest.mkdir(parents=True, mode=0o700)
      sources: list[Path] = []
      for vol_link in sorted(data_vols.iterdir()):
        # Resolve the run-dir volume *link* only. Contents are copied with cp -a,
        # which must not dereference symlinks *inside* the volume (silent corruption).
        source = vol_link.resolve()
        if not source.exists():
          raise _staging_failure(
            staging,
            f"App {app} data volume {vol_link.name} points at missing path: {source}",
          )
        sources.append(source)

      if sources:
        # One sudo invocation so the operator sees at most one password prompt.
        # -a: recursive, keep ownership/mode/times, preserve inner symlinks & hardlinks.
        logger.warning("Asking for sudo access to copy data volumes into snapshot.")
        sudo_cmd = [
          "sudo",
          "cp",
          "-a",
          "--",
          *[str(s) for s in sources],
          str(data_dest),
        ]
        logger.warning("Command is: %s", " ".join(sudo_cmd))
        result = subprocess.run(
          sudo_cmd,
          capture_output=True,
          text=True,
        )
        if result.returncode != 0:
          detail = (result.stderr or result.stdout or "").strip()
          message = (
            "Unable to create snapshot — because docker containers often write "
            "files as root, sudo is required to read volume contents. "
            "Ensure sudo is available and that you can authenticate when prompted."
          )
          if detail:
            message = f"{message}\n{detail}"
          raise _staging_failure(staging, message)

    snapshot_folder.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.move(str(staging), str(snapshot_folder))
    if staging.exists():
      shutil.rmtree(staging)
  except RuntimeError:
    raise
  except Exception as e:
    raise _staging_failure(staging, str(e)) from e

  sudo = bool(included)
  try:
    _tar_create(snapshot_folder, archive, sudo=sudo)
    remove_snapshot_dir(snapshot_folder, sudo=sudo)
  except Exception as e:
    raise RuntimeError(
      f"{e}\n"
      f"Uncompressed snapshot left at {snapshot_folder}; "
      f"remove it with `rm -rf {snapshot_folder}` before retrying."
    ) from e

  return archive
