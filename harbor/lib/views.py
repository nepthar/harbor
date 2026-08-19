"""JSON projections of harbor state.

The one rule here: **no secret ever leaves this module.** `AppRunData` carries
resolved config values, secrets included, because compose needs them. What a
viewer gets is the same thing `harbor inspect` prints -- a non-secret value, or
the fact that a secret is set -- and never the secret itself.

Shapes are shared by the daemon's HTTP API and the CLI's `--json` output, so
they stay in one place rather than being re-derived per front door.
"""

from __future__ import annotations

from typing import Any

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import AppObservation
from harbor.lib.receipt import published_route_urls
from harbor.lib.run_layout import AppRunData, load_run_data
from harbor.lib.stack import AppStack
from harbor.lib.util import path_size


def apps_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every installed app, in the shape a dashboard list wants.

  Deliberately excludes volume sizes: sizing a volume walks every file under
  it, which is fine for one app and pathological for a list that a browser
  polls. `app_view` pays that cost for the app you actually opened.
  """
  return [
    _summary(observation, ctx)
    for observation in ctx.observations()
    if observation.installed
  ]


def app_view(app_id: AppID, ctx: HarborCtx) -> dict[str, Any]:
  """One app in full: what `harbor inspect` shows, as data."""
  observation = _observation(app_id, ctx)
  stack = ctx.staged_stack(app_id)
  view = _summary(observation, ctx, stack=stack)

  if stack is None:
    return view

  run_data = load_run_data(stack, ctx)
  volumes = _volumes(stack, run_data, ctx)
  view.update(
    {
      "description": stack.description,
      "subdomain": stack.subdomain,
      "network_mode": stack.network_mode,
      "run_path": str(ctx.staged_paths(app_id).run_path),
      "manifest_stale": ctx.manifest_stale(app_id),
      "units": _units(stack, observation),
      "routes": _routes(stack, run_data, ctx),
      "volumes": volumes,
      "volume_bytes": sum(v["bytes"] for v in volumes if v["bytes"] is not None),
      "config": _config(stack, run_data, ctx),
      "commands": [
        {"name": name, "desc": command.desc, "unit": command.run_unit}
        for name, command in stack.commands.items()
      ],
      "issues": [
        {"problem": issue.problem, "fix": issue.fix}
        for issue in run_data.start_blockers
      ],
    }
  )
  return view


def _observation(app_id: AppID, ctx: HarborCtx) -> AppObservation:
  for observation in ctx.observations():
    if observation.app_id == app_id:
      return observation
  raise ValueError(f'No app state found for "{app_id}"')


def _summary(
  observation: AppObservation,
  ctx: HarborCtx,
  *,
  stack: AppStack | None = None,
) -> dict[str, Any]:
  if stack is None:
    stack = ctx.staged_stack(observation.app_id)
  return {
    "app_id": str(observation.app_id),
    "display_name": stack.display_name if stack else "",
    "version": stack.version if stack else None,
    "status": observation.status,
    "staged": observation.run_dir_exists and observation.compose_exists,
    "containers": {
      "running": observation.running_count,
      "total": len(observation.containers),
    },
    "configured": _configured(observation, stack, ctx),
    "volume_count": len(stack.volumes) if stack else 0,
    "last_action": observation.last_action,
  }


def _configured(
  observation: AppObservation, stack: AppStack | None, ctx: HarborCtx
) -> str | None:
  """ "ready", "missing", or None when the app has no config store yet.

  Same call `harbor ps` makes, so the dashboard and the terminal never
  disagree about whether an app is ready to start.
  """
  if not observation.config_exists or stack is None:
    return None
  return "missing" if load_run_data(stack, ctx).start_blockers else "ready"


def _units(stack: AppStack, observation: AppObservation) -> list[dict[str, Any]]:
  """Declared run units joined to whatever containers are actually up.

  Keyed on the `harbor.run_unit` label, so a unit the manifest declares but
  docker has never heard of shows up with a null state rather than vanishing.
  """
  containers = {c.run_unit: c for c in observation.containers}
  units = []
  for name, unit in stack.run_units.items():
    container = containers.get(name)
    units.append(
      {
        "name": name,
        "image": unit.image,
        "restart": unit.restart,
        "state": container.state if container else None,
        "container_name": container.name if container else None,
        "container_id": container.container_id if container else None,
      }
    )
  return units


def _routes(
  stack: AppStack, run_data: AppRunData, ctx: HarborCtx
) -> list[dict[str, Any]]:
  published = published_route_urls(stack, run_data, ctx)
  routes = []
  for name, route in stack.routes.items():
    assigned = run_data.routes.get(name)
    routes.append(
      {
        "name": name,
        "unit": route.run_unit_name,
        "desc": route.desc,
        "private": route.private,
        "scheme": route.scheme,
        "container_port": route.container_port,
        "host_port": assigned.host_port if assigned else None,
        "url": run_data.route_urls.get(name),
        "published_url": published.get(name),
      }
    )
  return routes


def _volumes(
  stack: AppStack, run_data: AppRunData, ctx: HarborCtx
) -> list[dict[str, Any]]:
  volumes = []
  for name, volume in stack.volumes.items():
    link = run_data.volume_links.get(name)
    source = link.source if link else None
    volumes.append(
      {
        "name": name,
        "kind": volume.kind,
        "readonly": volume.readonly,
        "path": str(source) if source else None,
        # `app` volumes live inside the run dir and are shipped with the happ,
        # so their size is a property of the download, not of what the app has
        # accumulated. Host volumes can be an entire media library.
        "bytes": path_size(source) if source and volume.kind != "host" else None,
      }
    )
  return volumes


def _config(
  stack: AppStack, run_data: AppRunData, ctx: HarborCtx
) -> list[dict[str, Any]]:
  store = ctx.app_store(stack.app)
  entries = []
  for name, config in stack.config.items():
    value = run_data.config_values.get(name)
    entries.append(
      {
        "name": name,
        "secret": config.secret,
        "desc": config.desc,
        "hidden": config.hidden,
        "set": store.has_config(name),
        "has_default": config.has_default(),
        # A secret's value is never projected -- not even when it is set, and
        # not even to say how long it is.
        "value": None if config.secret or value is None else value.value,
      }
    )
  return entries
