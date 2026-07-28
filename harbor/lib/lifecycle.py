from __future__ import annotations

import shutil
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

import yaml

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.docker import DockerError, docker_run_command
from harbor.lib.harbor import HarborCtx
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

logger = getLogger("harbor.lifecycle")


def _record(action: str, app_id: AppID, ctx: HarborCtx) -> None:
  record_app_action(action, app_id, ctx.config)


def _container_recovery_message(app_id: AppID, ctx: HarborCtx) -> str:
  containers = ctx.run_state(app_id).containers
  ids = ", ".join(container.container_id or container.name for container in containers)
  return (
    f"App {app_id} has Harbor-labeled containers but no usable compose.yml: {ids}. "
    "Refusing to remove state; recover or remove these containers manually."
  )


def _symlink_to(link: Path, target: Path) -> None:
  if link.is_symlink():
    link.unlink()
  elif link.exists():
    return
  link.symlink_to(target)


def _copy_to(source: Path, dest: Path) -> None:
  shutil.copy2(source, dest)


def _remove_managed_volumes(app_id: AppID, ctx: HarborCtx) -> None:
  for kind, root in ctx.config.volume_roots.items():
    app_dir = root / app_id
    if app_dir.is_dir():
      shutil.rmtree(app_dir)
      logger.info("removed %s volume %s", kind, app_dir)


def _materialize_run_structure(
  stack: AppStack, app_path: Path, run_data: AppRunData
) -> None:
  run_path = run_data.run_path

  # Make the run folder itself + $run/volumes
  run_path.mkdir(parents=True, exist_ok=True)
  volumes_path = run_path / "volumes"
  volumes_path.mkdir(exist_ok=True)
  for existing in volumes_path.iterdir():
    if existing.is_symlink() or existing.is_file():
      existing.unlink()
    else:
      shutil.rmtree(existing)

  # Copy in the manifest - so we can diff against it
  _copy_to(app_path / "manifest.toml", run_path / "manifest.toml")

  for volume_name, volume_link in run_data.volume_links.items():
    logger.debug(
      "volume %s: %s -> %s", volume_name, volume_link.source, volume_link.destination
    )
    if volume_link.mkdir:
      volume_link.source.mkdir(parents=True, exist_ok=True)
    if not volume_link.source.exists():
      raise ValueError(
        f"volume {volume_name} source does not exist: {volume_link.source}"
      )
    volume_link.destination.parent.mkdir(parents=True, exist_ok=True)
    _symlink_to(volume_link.destination, volume_link.source)

  with open(run_path / "compose.yml", "w") as f:
    yaml.safe_dump(make_compose_dict(stack, run_data), f, sort_keys=False)


def _generate_and_save_config(stack: AppStack, ctx: HarborCtx) -> None:
  app_db = ctx.app_db(stack.app)
  for config_name, config in stack.config.items():
    _, existing = app_db.get_config(config_name)
    if existing is None and config.default is not None:
      try:
        new_value = generate_secret(config.default) if config.secret else config.default
        app_db.set_config(config_name, config.secret, new_value)
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
  app_db = hdb.app_db(stack.app)

  app_db.clear_routes()
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

    hdb._store.write(f"routes/{stack.app}/{route_name}", assigned.__dict__)


@dataclass
class StageSuccess:
  stack: AppStack
  run_data: AppRunData


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


def stage(app: AppID, ctx: HarborCtx, source_path: Path) -> StageSuccess:
  """Generates all configuration possible (secrets, default config values), then
  materializes a staged app in harbor/run/<app_id>.

  Re-materializing from the same source refreshes generated files and volume
  links. A different source is refused (``harbor rm --runtime`` first). Nothing
  is written until validation passes, so a failed materialize leaves no run dir.
  """
  run_path = ctx.run_path(app)
  link = run_path / "source"

  if link.exists() and not link.is_symlink():
    raise ValueError(
      f"{link} exists but is not a symlink; remove it or run "
      f"`harbor rm --runtime {app}`"
    )
  if link.is_symlink():
    existing = link.readlink()
    if existing.resolve() != source_path.resolve():
      raise ValueError(
        f"App {app} is already installed from {existing}; run "
        f"`harbor rm --runtime {app}` before bringing up from {source_path}"
      )

  if not source_path.exists():
    raise ValueError(f"Source does not exist: {source_path}")

  try:
    running_count = ctx.run_state(app).running_count
  except ValueError:
    running_count = 0
  if running_count:
    raise ValueError(
      f"App {app} has {running_count} running Harbor-labeled container(s); "
      f"run `harbor down {app}` before `harbor up`"
    )

  stack = app_stack(source_path)

  _generate_and_save_config(stack, ctx)
  _clear_and_reallocate_ports(stack, ctx)

  run_data = load_run_data(stack, ctx, app_path=source_path)

  if run_data.stage_blockers:
    raise ValueError("\n".join(i.problem for i in run_data.stage_blockers))

  try:
    run_path.mkdir(parents=True, exist_ok=True)
    _symlink_to(link, source_path)
    _materialize_run_structure(stack, source_path, run_data)
    return StageSuccess(stack, run_data)
  except Exception:
    if run_path.exists():
      _record("up-failed", app, ctx)
    raise


def apply_config_sets(
  stack: AppStack, sets: list[tuple[str, str]], ctx: HarborCtx
) -> None:
  app_db = ctx.app_db(stack.app)
  for name, value in sets:
    config = stack.config.get(name)
    if not config:
      raise ValueError(f"No config {name} in {stack.app}'s manifest")
    if not value:
      raise ValueError(f"Empty value for config {name!r}")
    app_db.set_config(name, config.secret, value)


def bind(
  app: AppID,
  volname: str,
  host_path_str: str,
  ctx: HarborCtx,
  *,
  source_path: Path | None = None,
) -> None:
  """Record an external volume bind. Does not require materialization."""
  source = source_path or ctx.bundle_path(app)
  stack = app_stack(source)

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

  ctx.app_db(app).set_bind(volname, str(host_path), readonly=vol.readonly)


def up(
  app: AppID,
  ctx: HarborCtx,
  source_path: Path,
  *,
  sets: list[tuple[str, str]] | None = None,
  binds: list[tuple[str, str]] | None = None,
) -> StageSuccess:
  stack = app_stack(source_path)
  if sets:
    apply_config_sets(stack, sets, ctx)
  for volname, host_path in binds or []:
    bind(app, volname, host_path, ctx, source_path=source_path)

  result = stage(app, ctx, source_path)
  if result.run_data.start_blockers:
    lines = recovery_lines(app, result.run_data.start_blockers)
    raise ValueError("\n".join(lines))

  start(app, ctx)
  return result


def start(app: AppID, ctx: HarborCtx) -> None:
  """Start a runnable app via docker compose, then publish manifest routes."""
  state = ctx.run_state(app)
  if not state.compose_exists:
    raise ValueError(f"App {app} is not installed; run `harbor up {app}` first")

  run_path = state.run_path
  stack = app_stack(ctx.app_path(app))
  run_data = load_run_data(stack, ctx)
  if run_data.start_blockers:
    raise ValueError("\n".join(recovery_lines(app, run_data.start_blockers)))

  try:
    preflight_app_routes(run_data, ctx)
  except RouteProviderError as e:
    _record("up-failed", app, ctx)
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
    _record("up-failed", app, ctx)
    raise ValueError(str(e)) from e

  try:
    register_app_routes(stack, run_data, ctx)
  except RouteProviderError as e:
    _record("up-failed", app, ctx)
    raise ValueError(
      f"{e}. Containers may still be running; run `harbor down {app}` to stop them."
    ) from e

  _record("up", app, ctx)


def logs(app_id: AppID, extra_args: list[str], ctx: HarborCtx) -> None:
  """Stream ``docker compose logs`` for an installed app."""
  state = ctx.run_state(app_id)
  if not state.compose_exists:
    raise ValueError(f"App {app_id} is not installed; run `harbor up {app_id}` first")

  docker_run_command(
    ["compose", "logs", *(extra_args or [])],
    cwd=state.run_path,
    json_output=False,
    capture=False,
    check=True,
  )


def stop(app_id: AppID, ctx: HarborCtx) -> None:
  """Tear down routes, then bring an app's containers down."""
  state = ctx.run_state(app_id)
  if not state.compose_exists:
    if state.containers:
      raise ValueError(_container_recovery_message(app_id, ctx))
    raise ValueError(f"App {app_id} is not installed; run `harbor up {app_id}` first")

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
    )
    _record("down", app_id, ctx)
  except DockerError as e:
    _record("down-failed", app_id, ctx)
    raise ValueError(str(e)) from e


def unstage(app_id: AppID, ctx: HarborCtx) -> None:
  """Remove the run folder. Keeps volumes and appdb."""
  state = ctx.run_state(app_id)
  if state.containers:
    ids = ", ".join(
      container.container_id or container.name for container in state.containers
    )
    raise ValueError(
      f"App {app_id} still has Harbor-labeled containers ({ids}); "
      f"run `harbor down {app_id}` or remove them before unstaging"
    )

  run_path = state.run_path
  if run_path.exists():
    shutil.rmtree(run_path)
    logger.info("unstaged %s", app_id)
    return

  logger.warning(f"Run path did not exist, expected {run_path}")


def reset(app_id: AppID, ctx: HarborCtx) -> None:
  """Stop, unstage, and delete all persistent data + config for the app."""
  state = ctx.run_state(app_id)
  if state.containers and not state.compose_exists:
    raise ValueError(_container_recovery_message(app_id, ctx))

  if state.compose_exists:
    logger.info("Stopping %s", app_id)
    stop(app_id, ctx)

  if state.run_path.exists():
    shutil.rmtree(state.run_path)
    logger.info("unstaged %s", app_id)

  _remove_managed_volumes(app_id, ctx)
  ctx.harbor_db().purge_app(app_id)

  # The activity log outlives the app on purpose, so close it out rather than
  # leaving the trail ending at whatever happened before the removal.
  _record("purged", app_id, ctx)

  logger.info("reset %s", app_id)


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

  If two apps request the same subdomain, the first app `up` wins, and th
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


def register_app_routes(stack: AppStack, run_data: AppRunData, ctx: HarborCtx) -> None:
  web_routes = _web_routes(run_data)
  if not web_routes:
    return

  provider = get_route_provider(ctx.harbor_db(), ctx.config)
  domain = ctx.config.domain
  for route_name, route in web_routes:
    host_port = run_data.routes[route_name].host_port
    if host_port < 0:
      raise RouteProviderError(
        f"route {route_name!r} has no allocated host port; run `harbor up` first"
      )

    subdomain = route.subdomain
    provider.register_route(
      stack.app, host_port, subdomain, domain, scheme=route.scheme
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
  routes = ctx.app_db(app).list_routes()
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
