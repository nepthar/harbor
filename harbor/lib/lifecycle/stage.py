from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.happ import app_id_from_path, is_pathlike, load_happ
from harbor.lib.harbor import HarborCtx, StagedAppPaths
from harbor.lib.lifecycle._common import logger, managed_volume_dirs
from harbor.lib.logtab import LogTab
from harbor.lib.run_layout import (
  AppRunData,
  AssignedRoute,
  load_run_data,
  make_compose_dict,
)
from harbor.lib.secrets import SecretGenerationError, generate_secret
from harbor.lib.stack import AppStack

# Scratch names used while swapping in a new happ copy. Both are inside the run
# dir so the swap is a rename on one filesystem rather than a second copy.
INCOMING = ".happ.incoming"
OUTGOING = ".happ.outgoing"


def _has_volume_data(app_id: AppID, ctx: HarborCtx) -> bool:
  """Whether any managed volume holds something an app could depend on."""
  for app_dir in managed_volume_dirs(app_id, ctx):
    if not app_dir.is_dir():
      continue
    if any(not entry.is_dir() for entry in app_dir.rglob("*")):
      return True
  return False


def _stage_incoming(bundle: Path, run_path: Path) -> Path:
  """Extract the happ into ``run/<id>/.happ.incoming`` (not yet live).

  `load_happ` handles both bundle flavors, so a `.happ` directory is copied
  and a `.happ.md` file is expanded into the files it embeds; the run tree is
  always a plain directory either way.
  """
  happ = load_happ(bundle)
  incoming = run_path / INCOMING
  outgoing = run_path / OUTGOING
  for scratch in (incoming, outgoing):
    if scratch.exists():
      shutil.rmtree(scratch)

  happ.extract_to(incoming)
  return incoming


def _commit_incoming(paths: StagedAppPaths, incoming: Path) -> None:
  """Promote a validated incoming copy to ``happ/``."""
  outgoing = paths.run_path / OUTGOING
  happ = paths.happ_path
  if happ.exists():
    os.replace(happ, outgoing)
  os.replace(incoming, happ)
  if outgoing.exists():
    shutil.rmtree(outgoing)


def _discard_incoming(run_path: Path) -> None:
  """Drop a failed incoming copy; remove an empty run dir left behind."""
  incoming = run_path / INCOMING
  if incoming.exists():
    shutil.rmtree(incoming)
  if run_path.is_dir() and not any(run_path.iterdir()):
    run_path.rmdir()


def _generate_missing_config(stack: AppStack, ctx: HarborCtx) -> None:
  """Fill in defaults and generate secrets for all keys possible"""
  store = ctx.app_store(stack.app)
  for config_name, config in stack.config.items():
    if config.default is None or store.has_config(config_name):
      continue
    try:
      value = generate_secret(config.default) if config.secret else config.default
      store.set_config(config_name, config.secret, value)
    except SecretGenerationError as e:
      logger.error(f"{config_name}: {e}")


def _clear_and_reallocate_ports(stack: AppStack, ctx: HarborCtx) -> None:
  """Claim pinned host ports and allocate free ones in harbordb."""
  if stack.network_mode == "host" or not stack.routes:
    return

  has_web = any(route.publish == "web" for route in stack.routes.values())
  if has_web and not stack.subdomain:
    raise ValueError(f"App {stack.app} declares web routes but has no [app].subdomain")
  app_subdomain = stack.subdomain or ""

  hdb = ctx.harbor_db()
  hdb.clear_routes(stack.app)
  for route_name, route in stack.routes.items():
    if route.needs_allocation:
      host_port = hdb.next_free_port()
    else:
      host_port = route.host_port

    assigned = AssignedRoute(
      name=route_name,
      subdomain=route.subdomain(app_subdomain) if app_subdomain else "",
      run_unit_name=route.run_unit_name,
      host_port=host_port,
      container_port=route.container_port,
      proto=route.proto,
      publish=route.publish,
      scheme=route.scheme,
    )

    hdb.set_route(stack.app, route_name, assigned.__dict__)


def _existing_volume_kinds(volumes_root: Path) -> dict[str, str]:
  """volume name -> kind, read back from the links a previous stage left."""
  found: dict[str, str] = {}
  if not volumes_root.is_dir():
    return found
  for kind_dir in volumes_root.iterdir():
    if not kind_dir.is_dir():
      continue
    for link in kind_dir.iterdir():
      found[link.name] = kind_dir.name
  return found


def _make_link(destination: Path, target: Path) -> None:
  destination.parent.mkdir(parents=True, exist_ok=True)
  destination.symlink_to(target)


def _rebuild_volume_links(stack: AppStack, run_data: AppRunData) -> tuple[str, ...]:
  """Point `volumes/<kind>/<name>` at the current manifest's volumes.

  Note: ext/ volumes are linked in at run time as they may change between stage/run
  like configuration parameters or secrets.

  Returns the names the manifest no longer declares. Their links go; their data
  never does -- a manifest edit must not be able to delete bytes.
  """
  volumes_root = run_data.run_path / "volumes"
  existing = _existing_volume_kinds(volumes_root)

  for name, kind in existing.items():
    volume = stack.volumes.get(name)
    if volume is not None and volume.kind != kind:
      raise ValueError(
        f"App {stack.app} - volume {name} changed kind from {kind} to "
        f"{volume.kind}, but its data lives under the {kind} root. Move it by "
        f"hand, or run `harbor rm {stack.app}` to delete it."
      )

  # Only links live here; the data they point at is outside the run dir, or (for
  # app volumes) under happ/. So the whole tree can be torn down and rebuilt.
  if volumes_root.exists():
    shutil.rmtree(volumes_root)

  for volume_name, link in run_data.volume_links.items():
    if stack.volumes[volume_name].kind == "ext":
      continue
    logger.debug("volume %s: %s -> %s", volume_name, link.destination, link.target)
    if link.mkdir:
      link.source.mkdir(parents=True, exist_ok=True)
    if not link.source.exists():
      raise ValueError(f"volume {volume_name} source does not exist: {link.source}")
    _make_link(link.destination, link.target)

  return tuple(sorted(name for name in existing if name not in stack.volumes))


def link_ext_volumes(stack: AppStack, run_data: AppRunData) -> None:
  """Build `volumes/ext/` from the binds on file. Clobber existing links."""
  unlink_ext_volumes(run_data.run_path)
  for volume_name, link in run_data.volume_links.items():
    if stack.volumes[volume_name].kind != "ext":
      continue
    logger.debug("ext volume %s: %s -> %s", volume_name, link.destination, link.target)
    _make_link(link.destination, link.target)


def unlink_ext_volumes(run_path: Path) -> None:
  """Drop `volumes/ext/`. Only links are in there. I mean, unless YOU added something that you shouldn't."""
  ext_root = run_path / "volumes" / "ext"
  if ext_root.exists():
    shutil.rmtree(ext_root)


@dataclass(frozen=True)
class StageSuccess:
  stack: AppStack
  run_data: AppRunData
  dropped_volumes: tuple[str, ...] = ()


def materialize(stack: AppStack, ctx: HarborCtx) -> tuple[AppRunData, tuple[str, ...]]:
  """Rebuild everything derived from the happ now sitting in the run dir."""
  _clear_and_reallocate_ports(stack, ctx)

  run_data = load_run_data(stack, ctx)
  if run_data.stage_blockers:
    raise ValueError("\n".join(i.problem for i in run_data.stage_blockers))

  dropped = _rebuild_volume_links(stack, run_data)
  with open(ctx.staged_paths(stack.app).compose_path, "w") as f:
    yaml.safe_dump(make_compose_dict(stack, run_data), f, sort_keys=False)

  return run_data, dropped


@dataclass(frozen=True)
class StagingTarget:
  """What a `stage`/`start` argument named."""

  app_id: AppID
  # The bundle to stage: a `.happ` directory or a `.happ.md` file. None when
  # the argument was a bare id, which does not say which bundle it means --
  # `ctx.bundle_path` answers that, and only for a caller that has to stage.
  bundle: Path | None
  # The catalog entry created to reach the bundle, for the caller to report.
  linked_entry: Path | None


def staging_target(ctx: HarborCtx, target: str) -> StagingTarget:
  """Resolve a stage/start argument -- an app id, or a path to a bundle."""
  if not is_pathlike(target):
    return StagingTarget(ctx.resolve_app(target), None, None)

  bundle = Path(target).expanduser().resolve()
  app = app_id_from_path(bundle)
  catalogued = ctx.app_catalog().get(str(app), ())

  for entry in catalogued:
    if entry.path.resolve() == bundle:
      return StagingTarget(app, entry.path, None)

  # refuse when the id is already backed by a different
  # path, even if that entry is the other bundle flavor or another source.
  if catalogued:
    other = catalogued[0]
    raise ValueError(
      f"App {app} is already in the catalog as {other.path} -> "
      f"{other.path.resolve()}. Remove that entry to stage from {bundle} instead."
    )

  # The id comes from the bundle's own name, so the entry keeps the bundle's
  # flavor suffix (`.happ` directory or `.happ.md` file).
  link = ctx.config.apps_root / bundle.name
  if link.is_symlink() or link.exists():
    raise ValueError(
      f"App {app} is already in the catalog as {link} -> {link.resolve()}. "
      f"Remove that entry to stage from {bundle} instead."
    )

  link.parent.mkdir(parents=True, exist_ok=True)
  link.symlink_to(bundle)
  return StagingTarget(app, link, link)


def apply_config_sets(
  stack: AppStack, sets: list[tuple[str, str]], ctx: HarborCtx
) -> None:
  store = ctx.app_store(stack.app)
  for name, value in sets:
    config = stack.config.get(name)
    if not config:
      raise ValueError(f"No config {name} in {stack.app}'s manifest")
    if not value:
      raise ValueError(f"Empty value for config {name!r}")
    store.set_config(name, config.secret, value)


def bind(stack: AppStack, volname: str, host_path_str: str, ctx: HarborCtx) -> None:
  """Record an external volume bind against the staged happ.

  Recording is all it does; `start` turns the binds on file into the links
  under `volumes/ext/`.
  """
  app = stack.app

  if volname not in stack.volumes:
    raise ValueError(f"App {app} - no such volume {volname}")

  vol = stack.volumes[volname]
  if vol.kind != "ext":
    raise ValueError(
      f"App {app} - volume {volname}, kind={vol.kind}, only ext volumes can be bound"
    )

  host_path = Path(host_path_str).expanduser().resolve()
  if not host_path.exists():
    raise ValueError(f"App {app} - Path does not exist: {host_path_str}")

  ctx.app_store(app).set_bind(volname, str(host_path), readonly=vol.readonly)


def stage(
  app: AppID,
  bundle: Path,
  ctx: HarborCtx,
  *,
  sets: list[tuple[str, str]] | None = None,
  binds: list[tuple[str, str]] | None = None,
) -> StageSuccess:
  """Install `bundle` into `run/<id>/` without starting it.

  `bundle` is the happ itself -- a `.happ` directory or a `.happ.md` file --
  and is recorded as the app's origin. Which app source it happens to sit in
  is a fact about that path, derivable when anyone needs it, so nothing here
  carries a catalog around.

  The happ copy, the volume links, the routes and compose.yml are all rebuilt
  from the manifest every time. Config values and volume *contents* are not:
  they belong to the installation, not the bundle (docs/run-layout.md §5).
  """
  paths = ctx.staged_paths(app)

  try:
    running_count = ctx.run_state(app).running_count
  except ValueError:
    running_count = 0
  if running_count:
    raise ValueError(
      f"App {app} has {running_count} running Harbor-labeled container(s); "
      f"run `harbor stop {app}` first"
    )

  # Config gone while the data it belongs to is still here means someone
  # deleted the run dir by hand. Staging would generate fresh `auto` secrets
  # against data expecting the old ones, so refuse instead.
  if not paths.config_path.is_file() and _has_volume_data(app, ctx):
    raise ValueError(
      f"App {app} has volume data but no config at {paths.config_path}. "
      f"Staging would generate new secrets that its existing data does not "
      f"expect. Restore the run directory from a backup, or run "
      f"`harbor rm {app}` to delete its config and data together."
    )

  # Extract the happ under run/ first, validate *that* copy, then promote it
  # to happ/. AppStack always comes from the run tree, never the bundle.
  run_path = paths.run_path
  run_path.mkdir(parents=True, exist_ok=True)
  try:
    incoming = _stage_incoming(bundle, run_path)
    stack = AppStack.from_file(incoming / "manifest.toml", app)
  except Exception:
    _discard_incoming(run_path)
    raise
  _commit_incoming(paths, incoming)

  store = ctx.app_store(app)
  store.set_meta("origin", str(bundle))

  # Apply configuration sets if we're given them
  if sets:
    apply_config_sets(stack, sets, ctx)

  # Apply binds, if we're given them
  if binds:
    for volname, host_path in binds:
      bind(stack, volname, host_path, ctx)

  _generate_missing_config(stack, ctx)

  try:
    run_data, dropped = materialize(stack, ctx)
  except Exception:
    record_app_action("stage-failed", app, ctx.config)
    raise

  store.set_meta("staged_at", LogTab.ts())
  record_app_action("staged", app, ctx.config)
  return StageSuccess(stack, run_data, dropped)
