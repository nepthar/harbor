from __future__ import annotations

import shlex
import shutil
from datetime import UTC, datetime
from logging import getLogger
from pathlib import Path

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle.rootfs import run_as_root
from harbor.lib.stack import AppStack
from harbor.lib.util import validate_identifier

logger = getLogger("harbor.lifecycle.snapshot")

SNAPSHOT_TAR_SUFFIX = ".tar.gz"
SNAPSHOT_STAMP = "%Y-%m-%d_%H-%MZ"


def split_snapshot_name(name: str) -> tuple[str | None, str]:
  """ISO timestamp and label encoded in a snapshot folder name.

  `2026-08-25_23-19Z` has no label. `2026-08-25_23-19Z_pre-restore` does.
  """
  date, sep, rest = name.partition("_")
  if not sep:
    return None, ""
  clock, _, label = rest.partition("_")
  try:
    when = datetime.strptime(f"{date}_{clock}", SNAPSHOT_STAMP).replace(tzinfo=UTC)
  except ValueError:
    return None, ""
  return when.isoformat(timespec="seconds").replace("+00:00", "Z"), label


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


def _tar_create(folder: Path, archive: Path) -> None:
  """Archive `folder` into `archive`, with the *host* owning the archive file.

  The container only writes the compressed stream to stdout; this process opens
  the destination, so the archive belongs to the operator and not to root.
  """
  folder = folder.resolve()
  parent = shlex.quote(str(folder.parent))
  script = f"tar -czf - -C {parent} {shlex.quote(folder.name)}"
  try:
    with open(archive, "wb") as out:
      run_as_root(f"compress the snapshot at {folder}", script, [folder], stdout=out)
  except Exception:
    archive.unlink(missing_ok=True)
    raise


def _tar_extract(archive: Path, dest_parent: Path) -> None:
  archive = archive.resolve()
  dest_parent = dest_parent.resolve()
  script = f"tar -xzf {shlex.quote(str(archive))} -C {shlex.quote(str(dest_parent))}"
  run_as_root(f"extract the snapshot {archive}", script, [archive.parent, dest_parent])


def remove_snapshot_dir(folder: Path) -> None:
  """Delete an uncompressed snapshot folder.

  Only its volume tree can hold root-owned files -- the manifest, config and
  happ copy were written by this process. A snapshot of an app with no data
  volumes is therefore ours to delete, and paying for a container to do it
  would make cleanup fail whenever docker is down.
  """
  if not folder.exists():
    return
  folder = folder.resolve()
  if not any((folder / "volumes").rglob("*")):
    shutil.rmtree(folder)
    return
  script = f"rm -rf -- {shlex.quote(str(folder))}"
  run_as_root(f"remove {folder}", script, [folder.parent])


def extract_snapshot(archive: Path) -> Path:
  name = archive.name.removesuffix(SNAPSHOT_TAR_SUFFIX)
  folder = archive.parent / name
  if folder.exists():
    raise ValueError(
      f"Incomplete snapshot extract left at {folder}; "
      f"remove it with `rm -rf {folder}` before retrying."
    )
  try:
    _tar_extract(archive, archive.parent)
  except Exception:
    # Cleanup must not speak over the failure it is cleaning up after: when
    # docker is the thing that broke, removal fails too, and its message would
    # replace the only one that says what actually went wrong.
    try:
      remove_snapshot_dir(folder)
    except Exception as cleanup_error:
      logger.warning(
        "could not remove %s after a failed extract: %s", folder, cleanup_error
      )
    raise
  return folder


def snapshot(
  app: AppID,
  ctx: HarborCtx,
  label: str = "",
) -> Path:
  """Copy a stopped app's happ, config, and data volumes into an archive.

  Assumes the caller holds the app lock and the app is stopped.
  """
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
  for file in (paths.manifest_path, ctx.config.app_config_path(app)):
    if not file.is_file():
      raise ValueError(
        f"App {app} missing required file: {file}. "
        "This app appears to be staged improperly"
      )

  folder_name = datetime.now(UTC).strftime(SNAPSHOT_STAMP)
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

  staging = ctx.config.temp_root / "current_snapshot"
  if staging.exists():
    raise ValueError(
      f"Incomplete snapshot left at {staging}; "
      f"remove it with `rm -rf {staging}` before retrying."
    )

  staging.mkdir(parents=True, mode=0o700)

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
    shutil.copy2(ctx.config.app_config_path(app), staging / "config.logtab")
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
        # -a: recursive, keep ownership/mode/times, preserve inner symlinks &
        # hardlinks. The sources are already-resolved link targets; -a must not
        # dereference the symlinks *inside* them.
        quoted = " ".join(shlex.quote(str(s)) for s in sources)
        script = f"cp -a -- {quoted} {shlex.quote(str(data_dest.resolve()))}"
        try:
          run_as_root(
            f"copy the data volumes of {app} into the snapshot",
            script,
            [*sources, data_dest],
          )
        except RuntimeError as e:
          raise _staging_failure(staging, str(e)) from e

    snapshot_folder.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.move(str(staging), str(snapshot_folder))
    if staging.exists():
      shutil.rmtree(staging)
  except RuntimeError:
    raise
  except Exception as e:
    raise _staging_failure(staging, str(e)) from e

  try:
    _tar_create(snapshot_folder, archive)
    remove_snapshot_dir(snapshot_folder)
  except Exception as e:
    raise RuntimeError(
      f"{e}\n"
      f"Uncompressed snapshot left at {snapshot_folder}; "
      f"remove it with `rm -rf {snapshot_folder}` before retrying."
    ) from e

  return archive
