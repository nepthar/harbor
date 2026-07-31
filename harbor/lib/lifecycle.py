from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from logging import getLogger
from pathlib import Path

import yaml

from harbor.lib.appconfig import config_path
from harbor.lib.apps import AppID, app_id_from_path, is_pathlike, record_app_action
from harbor.lib.docker import DockerError, docker_run_command
from harbor.lib.harbor import HarborCtx
from harbor.lib.manifest import ConfigError
from harbor.lib.routes import (
  RouteProviderError,
  get_route_provider,
  refuse_foreign_route,
)
from harbor.lib.run_layout import (
  AppRunData,
  AssignedRoute,
  ConfigIssue,
  load_run_data,
  make_compose_dict,
)
from harbor.lib.secrets import SecretGenerationError, generate_secret
from harbor.lib.stack import AppStack, app_stack
from harbor.lib.util import Conn

logger = getLogger("harbor.lifecycle")

# Scratch names used while swapping in a new happ copy. Both are inside the run
# dir so the swap is a rename on one filesystem rather than a second copy.
INCOMING = ".happ.incoming"
OUTGOING = ".happ.outgoing"


def _record(action: str, app_id: AppID, ctx: HarborCtx) -> None:
  record_app_action(action, app_id, ctx.config)


def _container_recovery_message(app_id: AppID, ctx: HarborCtx) -> str:
  containers = ctx.run_state(app_id).containers
  ids = ", ".join(container.container_id or container.name for container in containers)
  return (
    f"App {app_id} has Harbor-labeled containers but no usable compose.yml: {ids}. "
    "Refusing to remove state; recover or remove these containers manually."
  )


def _managed_volume_dirs(app_id: AppID, ctx: HarborCtx) -> list[Path]:
  return [root / app_id for root in ctx.config.volume_roots.values()]


def _has_volume_data(app_id: AppID, ctx: HarborCtx) -> bool:
  """Whether any managed volume holds something an app could depend on."""
  for app_dir in _managed_volume_dirs(app_id, ctx):
    if not app_dir.is_dir():
      continue
    if any(not entry.is_dir() for entry in app_dir.rglob("*")):
      return True
  return False


def _swap_happ(catalog: Path, run_path: Path) -> None:
  """Copy the catalog entry in, then move it into place.

  `happ/` is never edited in place: a copy that fails half way through would
  otherwise leave a bundle that is neither the old app nor the new one. Inner
  symlinks are dereferenced so the run copy is self-contained.
  """
  incoming = run_path / INCOMING
  outgoing = run_path / OUTGOING
  for scratch in (incoming, outgoing):
    if scratch.exists():
      shutil.rmtree(scratch)

  shutil.copytree(catalog, incoming)
  happ = run_path / "happ"
  if happ.exists():
    os.replace(happ, outgoing)
  os.replace(incoming, happ)
  if outgoing.exists():
    shutil.rmtree(outgoing)


def _generate_missing_config(stack: AppStack, ctx: HarborCtx) -> None:
  """Fill in defaults and `auto` secrets, for keys that have no value yet.

  Only the missing ones. Re-staging must never mint a new secret over one the
  app's existing data already depends on, which fails as an authentication
  error that nothing in the app explains (docs/run-layout.md §5 step 5).
  """
  store = ctx.app_config(stack.app)
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


def _rebuild_volume_links(stack: AppStack, run_data: AppRunData) -> tuple[str, ...]:
  """Point `volumes/<kind>/<name>` at the current manifest's volumes.

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
    logger.debug("volume %s: %s -> %s", volume_name, link.destination, link.target)
    if link.mkdir:
      link.source.mkdir(parents=True, exist_ok=True)
    if not link.source.exists():
      raise ValueError(f"volume {volume_name} source does not exist: {link.source}")
    link.destination.parent.mkdir(parents=True, exist_ok=True)
    link.destination.symlink_to(link.target)

  return tuple(sorted(name for name in existing if name not in stack.volumes))


@dataclass(frozen=True)
class StageSuccess:
  stack: AppStack
  run_data: AppRunData
  dropped_volumes: tuple[str, ...] = ()


def recovery_lines(app_id: AppID, issues: tuple[ConfigIssue, ...]) -> list[str]:
  """Turn start blockers into what is wrong and how to fix it.

  Naming the problem matters as much as the remedy: several issues share the
  same fix, so listing fixes alone produced repeated lines that never said
  which value or volume was at fault.
  """
  lines = [f"{app_id} cannot start:"]
  for issue in issues:
    lines.append(f"  - {issue.problem}")
    if issue.fix:
      lines.append(f"    {issue.fix}")
  return lines


def catalog_entry(ctx: HarborCtx, target: str) -> tuple[AppID, Path | None]:
  """Resolve a stage/start target to an app id backed by an `apps/` entry.

  A path argument is symlinked into the catalog first, so `apps/` is literally
  the only thing staging copies from and a developer's checkout keeps working
  (docs/run-layout.md L14). Returns the entry created, if any, so the caller
  can say so.
  """
  if not is_pathlike(target):
    return ctx.resolve_app(target), None

  source = Path(target).expanduser().resolve()
  app = app_id_from_path(source)
  entry = ctx.config.apps_root / f"{app}.happ"

  if entry.is_symlink() or entry.exists():
    if entry.resolve() != source:
      raise ValueError(
        f"App {app} is already in the catalog as {entry} -> {entry.resolve()}. "
        f"Remove that entry to stage from {source} instead."
      )
    return app, None

  entry.parent.mkdir(parents=True, exist_ok=True)
  entry.symlink_to(source)
  return app, entry


def snapshot(
  app: AppID,
  ctx: HarborCtx,
  conn: Conn,
  label: str = "",
) -> Path:

  run_path = ctx.run_path(app)

  if not run_path.exists():
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

  # Required files for the snapshot. If these don't exist, something is wrong with the app.
  run_manifest = run_path / "happ" / "manifest.toml"
  config_logtab = config_path(run_path)
  compose_yml = run_path / "compose.yml"

  for file in (run_manifest, config_logtab, compose_yml):
    if not file.is_file():
      raise ValueError(f"App {app} missing required file: {file}. This app appears to be staged improperly")

  # Create the snapshot directory.
  folder_name = datetime.now().strftime("%Y-%m-%d_%H-%M")
  if label:
    folder_name = f"{folder_name}-{label}"

  snapshot_folder = ctx.config.snapshot_root / app / folder_name
  if snapshot_folder.exists():
    raise ValueError(f"Snapshot folder already exists: {snapshot_folder}. Are you taking another snapshot in the same minute?")
  snapshot_folder.mkdir(parents=True, mode=0o700)

  # Config and compose are harbor-owned; copy2 keeps mode and mtime. Secrets stay
  # Fernet ciphertext — we never decrypt on this path.
  shutil.copy2(config_logtab, snapshot_folder / "config.logtab")
  shutil.copy2(compose_yml, snapshot_folder / "compose.yml")
  shutil.copytree(run_path / "happ", snapshot_folder / "happ")

  data_vols = run_path / "volumes" / "data"
  if data_vols.is_dir():
    data_dest = snapshot_folder / "volumes" / "data"
    data_dest.mkdir(parents=True, mode=0o700)
    sources: list[Path] = []
    for vol_link in sorted(data_vols.iterdir()):
      # Resolve the run-dir volume *link* only. Contents are copied with cp -a,
      # which must not dereference symlinks *inside* the volume (silent corruption).
      source = vol_link.resolve()
      if not source.exists():
        raise ValueError(
          f"App {app} data volume {vol_link.name} points at missing path: {source}"
        )
      sources.append(source)

    if sources:
      # One sudo invocation so the operator sees at most one password prompt.
      # -a: recursive, keep ownership/mode/times, preserve inner symlinks & hardlinks.
      conn.out("sudo access is required to copy data volumes into snapshot.")
      result = subprocess.run(
        ["sudo", "cp", "-a", "--", *[str(s) for s in sources], str(data_dest)],
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
        raise RuntimeError(message)

  return snapshot_folder

def stage(
  app: AppID,
  ctx: HarborCtx,
  *,
  sets: list[tuple[str, str]] | None = None,
  binds: list[tuple[str, str]] | None = None,
) -> StageSuccess:
  """Install `apps/<id>.happ` into `run/<id>/` without starting it.

  The happ copy, the volume links, the routes and compose.yml are all rebuilt
  from the manifest every time. Config values and volume *contents* are not:
  they belong to the installation, not the bundle (docs/run-layout.md §5).
  """
  run_path = ctx.run_path(app)
  catalog = ctx.bundle_path(app)

  try:
    running_count = ctx.run_state(app).running_count
  except ValueError:
    running_count = 0
  if running_count:
    raise ValueError(
      f"App {app} has {running_count} running Harbor-labeled container(s); "
      f"run `harbor stop {app}` first"
    )

  stack = app_stack(catalog, app)

  # Config gone while the data it belongs to is still here means someone
  # deleted the run dir by hand. Staging would generate fresh `auto` secrets
  # against data expecting the old ones, so refuse instead.
  if not config_path(run_path).is_file() and _has_volume_data(app, ctx):
    raise ValueError(
      f"App {app} has volume data but no config at {config_path(run_path)}. "
      f"Staging would generate new secrets that its existing data does not "
      f"expect. Restore the run directory from a backup, or run "
      f"`harbor rm {app}` to delete its config and data together."
    )

  run_path.mkdir(parents=True, exist_ok=True)
  _swap_happ(catalog, run_path)

  if sets:
    apply_config_sets(stack, sets, ctx)
  for volname, host_path in binds or []:
    bind(stack, volname, host_path, ctx)

  _generate_missing_config(stack, ctx)
  _clear_and_reallocate_ports(stack, ctx)

  run_data = load_run_data(stack, ctx)
  if run_data.stage_blockers:
    raise ValueError("\n".join(i.problem for i in run_data.stage_blockers))

  try:
    dropped = _rebuild_volume_links(stack, run_data)
    with open(run_path / "compose.yml", "w") as f:
      yaml.safe_dump(make_compose_dict(stack, run_data), f, sort_keys=False)
  except Exception:
    _record("stage-failed", app, ctx)
    raise

  store = ctx.app_config(app)
  store.set_meta("origin", str(catalog))
  store.set_meta("staged_at", datetime.now().astimezone().isoformat(timespec="seconds"))
  _record("staged", app, ctx)
  return StageSuccess(stack, run_data, dropped)


def apply_config_sets(
  stack: AppStack, sets: list[tuple[str, str]], ctx: HarborCtx
) -> None:
  store = ctx.app_config(stack.app)
  for name, value in sets:
    config = stack.config.get(name)
    if not config:
      raise ValueError(f"No config {name} in {stack.app}'s manifest")
    if not value:
      raise ValueError(f"Empty value for config {name!r}")
    store.set_config(name, config.secret, value)


def bind(stack: AppStack, volname: str, host_path_str: str, ctx: HarborCtx) -> None:
  """Record an external volume bind against the staged happ."""
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

  ctx.app_config(app).set_bind(volname, str(host_path), readonly=vol.readonly)


def start(
  app: AppID,
  ctx: HarborCtx,
  *,
  sets: list[tuple[str, str]] | None = None,
  binds: list[tuple[str, str]] | None = None,
) -> StageSuccess:
  """Stage if needed, then bring the app up and publish its web routes.

  `--set` and `--bind` re-stage, because config and binds are inputs to the
  volume links and compose file that staging generates.
  """
  if sets or binds or not ctx.is_staged(app):
    result = stage(app, ctx, sets=sets, binds=binds)
  else:
    stack = app_stack(ctx.app_path(app), app)
    result = StageSuccess(stack, load_run_data(stack, ctx))

  stack, run_data = result.stack, result.run_data
  if run_data.start_blockers:
    raise ValueError("\n".join(recovery_lines(app, run_data.start_blockers)))

  run_path = ctx.run_path(app)
  if not (run_path / "compose.yml").is_file():
    raise ValueError(f"App {app} is not staged; run `harbor stage {app}` first")

  try:
    preflight_app_routes(run_data, ctx)
  except RouteProviderError as e:
    _record("start-failed", app, ctx)
    raise ValueError(str(e)) from e

  try:
    docker_run_command(
      ["compose", "up", "-d"],
      cwd=run_path,
      json_output=False,
      check=True,
      env=run_data.config_env(),
    )
  except DockerError as e:
    _record("start-failed", app, ctx)
    raise ValueError(str(e)) from e

  try:
    register_app_routes(run_data, ctx)
  except RouteProviderError as e:
    _record("start-failed", app, ctx)
    raise ValueError(
      f"{e}. Containers may still be running; run `harbor stop {app}` to stop them."
    ) from e

  _record("start", app, ctx)
  return result


def _compose_env(app_id: AppID, ctx: HarborCtx) -> dict[str, str]:
  """The config environment compose.yml interpolates `${__HARBOR_CONFIG__*}` from.

  Every compose invocation needs it, not just `up`: without it compose warns
  about each unset variable and renders them blank, so `down` and `logs` would
  be reasoning about a different project definition than `up` created. Best
  effort -- a broken or half-removed app must still be stoppable, so a stack
  that will not parse falls back to no env rather than blocking teardown.
  """
  try:
    stack = app_stack(ctx.app_path(app_id), app_id)
    return load_run_data(stack, ctx).config_env()
  except (ValueError, ConfigError) as e:
    logger.debug("no config env for %s: %s", app_id, e)
    return {}


def logs(app_id: AppID, extra_args: list[str], ctx: HarborCtx) -> None:
  """Stream ``docker compose logs`` for a staged app."""
  state = ctx.run_state(app_id)
  if not state.compose_exists:
    raise ValueError(f"App {app_id} is not staged; run `harbor stage {app_id}` first")

  docker_run_command(
    ["compose", "logs", *(extra_args or [])],
    cwd=state.run_path,
    json_output=False,
    check=True,
    env=_compose_env(app_id, ctx),
  )


def stop(app_id: AppID, ctx: HarborCtx) -> None:
  """Tear down routes, then bring an app's containers down."""
  state = ctx.run_state(app_id)
  if not state.compose_exists:
    if state.containers:
      raise ValueError(_container_recovery_message(app_id, ctx))
    raise ValueError(f"App {app_id} is not staged; run `harbor stage {app_id}` first")

  try:
    unregister_app_routes(app_id, ctx)
  except Exception as e:
    logger.error("failed to unregister routes for %s: %s", app_id, e)

  try:
    docker_run_command(
      ["compose", "down"],
      cwd=state.run_path,
      json_output=False,
      check=True,
      env=_compose_env(app_id, ctx),
    )
    _record("stop", app_id, ctx)
  except DockerError as e:
    _record("stop-failed", app_id, ctx)
    raise ValueError(str(e)) from e


@dataclass(frozen=True)
class RemovalPlan:
  """What `harbor rm` will delete, and what it deliberately will not."""

  app_id: AppID
  run_path: Path
  volume_paths: tuple[Path, ...]
  ext_paths: tuple[Path, ...]
  stop_first: bool


def removal_plan(app_id: AppID, ctx: HarborCtx) -> RemovalPlan:
  """Work out what removing an app would destroy, without destroying it."""
  state = ctx.run_state(app_id)
  if state.containers and not state.compose_exists:
    raise ValueError(_container_recovery_message(app_id, ctx))

  ext_paths: tuple[Path, ...] = ()
  if config_path(state.run_path).is_file():
    ext_paths = tuple(
      Path(entry["host_path"]) for entry in ctx.app_config(app_id).list_binds().values()
    )

  return RemovalPlan(
    app_id=app_id,
    run_path=state.run_path,
    volume_paths=tuple(d for d in _managed_volume_dirs(app_id, ctx) if d.is_dir()),
    ext_paths=ext_paths,
    stop_first=state.compose_exists,
  )


def rm(plan: RemovalPlan, ctx: HarborCtx) -> None:
  """Stop an app and delete its run dir, managed volumes and route claims.

  The catalog entry and any `ext` volume contents survive, so
  `harbor rm foo; harbor start foo` is a clean reinstall.

  TODO(docs/run-layout.md §8): capture a snapshot and verify its checksum
  before the first byte is deleted, once `harbor snapshot` exists. Until then
  this is unrecoverable and the CLI says so.
  """
  app_id = plan.app_id
  if plan.stop_first:
    logger.info("Stopping %s", app_id)
    stop(app_id, ctx)

  if plan.run_path.exists():
    shutil.rmtree(plan.run_path)
    logger.info("removed run directory %s", plan.run_path)

  for path in plan.volume_paths:
    if path.is_dir():
      shutil.rmtree(path)
      logger.info("removed volume %s", path)

  ctx.harbor_db().purge_app(app_id)

  # The activity log outlives the app on purpose, so close it out rather than
  # leaving the trail ending at whatever happened before the removal.
  _record("removed", app_id, ctx)
  logger.info("removed %s", app_id)


def _web_routes(run_data: AppRunData) -> list[tuple[str, AssignedRoute]]:
  routes = []
  for route_name, route in run_data.routes.items():
    if route.publish != "web":
      logger.debug(
        "route %s is %s, not web-facing; skipping", route_name, route.publish
      )
      continue
    routes.append((route_name, route))
  return routes


def preflight_app_routes(run_data: AppRunData, ctx: HarborCtx) -> None:
  """Sanity check that the routes requested by the run data can be satisfied

  If two apps request the same subdomain, the first app started wins, and the
  second fails here.
  """
  web_routes = _web_routes(run_data)
  if not web_routes:
    return

  provider = get_route_provider(ctx.harbor_db(), ctx.config)
  owners = provider.route_owners()
  domain = ctx.config.domain
  for _, route in web_routes:
    subdomain = route.subdomain
    if subdomain not in owners:
      continue
    owner = owners[subdomain]
    if owner == run_data.app:
      continue
    raise refuse_foreign_route(f"{subdomain}.{domain}", owner)


def register_app_routes(run_data: AppRunData, ctx: HarborCtx) -> None:
  web_routes = _web_routes(run_data)
  if not web_routes:
    return

  provider = get_route_provider(ctx.harbor_db(), ctx.config)
  domain = ctx.config.domain
  for route_name, route in web_routes:
    host_port = run_data.routes[route_name].host_port
    if host_port < 0:
      raise RouteProviderError(
        f"route {route_name!r} has no allocated host port; run `harbor stage` first"
      )

    subdomain = route.subdomain
    provider.register_route(
      run_data.app, host_port, subdomain, domain, scheme=route.scheme
    )
    logger.info(
      "registered route %s: %s.%s -> %s://:%d",
      route_name,
      subdomain,
      domain,
      route.scheme,
      host_port,
    )


def unregister_app_routes(app: AppID, ctx: HarborCtx) -> None:
  routes = ctx.harbor_db().list_routes(app)
  published = [
    AssignedRoute(**route) for route in routes.values() if route["publish"] == "web"
  ]
  provider = get_route_provider(ctx.harbor_db(), ctx.config)
  domain = ctx.config.domain
  for route in published:
    try:
      provider.unregister_route(route.subdomain, domain)
      logger.info("unregistered route %s: %s.%s", route.name, route.subdomain, domain)
    except RouteProviderError as e:
      logger.error(
        "failed to unregister route %s for %s: %s",
        route.name,
        app,
        e,
      )
