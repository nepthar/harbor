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
from harbor.lib.happ import HAPP_MD_SUFFIX, extract_md_files, load_happ
from harbor.lib.harbor import CatalogEntry, HarborCtx
from harbor.lib.observations import AppObservation
from harbor.lib.receipt import published_route_urls
from harbor.lib.run_layout import AppRunData, load_run_data
from harbor.lib.stack import AppStack
from harbor.lib.store import AppStore
from harbor.lib.util import path_size

GITHUB_PREFIX = "github:"


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


def catalog_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every configured app source, with the happs currently in it.

  Grouped here so a browser can render one table per catalog without
  re-deriving the grouping. A bundle whose manifest does not parse still
  appears: dropping it would hide a problem the operator needs to see.
  """
  catalogs: dict[str, list[dict[str, Any]]] = {
    name: [] for name in ctx.config.app_sources
  }
  catalog = ctx.app_catalog()
  for app_id in sorted(catalog):
    for entry in catalog[app_id]:
      catalogs.setdefault(entry.source, []).append(_catalog_app(entry, ctx))
  return [{"name": name, "apps": apps} for name, apps in catalogs.items()]


def _catalog_app(entry: CatalogEntry, ctx: HarborCtx) -> dict[str, Any]:
  stack = _catalog_stack(entry)
  # AppStore creates the logtab on contact; a listing must not invent one.
  store = None
  if stack is not None and ctx.config.app_config_path(stack.app).is_file():
    store = ctx.app_store(stack.app)
  return {
    "app_id": entry.app_id,
    "display_name": stack.display_name if stack else "",
    "version": stack.version if stack else None,
    "description": stack.description if stack else "",
    "source": _catalog_origin(entry.app_id, ctx),
    "catalog": entry.source,
    "configured": _catalog_configured(stack, store) if stack else None,
    "manifest": _catalog_manifest(entry),
  }


def _catalog_manifest(entry: CatalogEntry) -> str:
  """The happ's manifest.toml as it sits on disk, parseable or not."""
  path = entry.path
  if path.is_dir():
    try:
      return (path / "manifest.toml").read_text()
    except OSError:
      return ""
  if path.name.endswith(HAPP_MD_SUFFIX):
    try:
      files = extract_md_files(path.read_text())
    except OSError:
      return ""
    for md_file in files.files:
      if md_file.path == "manifest.toml":
        return md_file.content
  return ""


def _catalog_configured(stack: AppStack, store: AppStore | None) -> str:
  """ "ready" when every config key is set or will be filled, else "missing".

  A secret with a default is generated on stage, so it does not yellow the
  card. An unset key with no default is the operator's to supply.
  """
  for name, cfg in stack.config.items():
    if cfg.default is not None:
      continue
    if store is not None and store.has_config(name):
      continue
    return "missing"
  return "ready"


def _catalog_stack(entry: CatalogEntry) -> AppStack | None:
  """The bundle's schema, or None when the happ on disk does not parse.

  Same swallow as `HarborCtx.staged_stack`: a listing must not die on one
  broken app.
  """
  try:
    return load_happ(entry.path).app_stack()
  except (ValueError, RuntimeError):
    return None


def _catalog_origin(app_id: str, ctx: HarborCtx) -> str:
  """Where this happ was fetched from, collapsed for a table cell.

  Fetch records the full github: spec; the catalog only needs the publisher.
  Anything else -- a path, a missing record, a spec we cannot read -- is local.
  """
  record = ctx.harbor_db().get_app_source(app_id)
  if record is None:
    return "local"
  spec = record["source"]
  if not spec.startswith(GITHUB_PREFIX):
    return "local"
  user = spec[len(GITHUB_PREFIX) :].split("/", 1)[0]
  return f"{GITHUB_PREFIX}{user}" if user else "local"


def volumes_view(ctx: HarborCtx, *, sizes: bool = False) -> list[dict[str, Any]]:
  """Every harbor-managed volume on disk, whatever declared it.

  Read from the volume roots rather than from manifests, so data an app left
  behind still shows up -- `rm` deletes the run dir and keeps the volume, and
  that data is invisible everywhere else.

  `sizes` walks every file under every volume. The cost tracks file *count*,
  not bytes, so it is usually quick and occasionally not; the caller decides
  when to pay it.
  """
  running = {
    observation.app_id
    for observation in ctx.observations()
    if observation.running_count
  }
  declared: dict[str, set[str]] = {}
  volumes = []

  for kind, root in sorted(ctx.config.volume_roots.items()):
    if not root.is_dir():
      continue
    for app_dir in sorted(root.iterdir()):
      if not app_dir.is_dir():
        continue
      app_id = app_dir.name
      if app_id not in declared:
        stack = ctx.staged_stack(app_id)
        declared[app_id] = set(stack.volumes) if stack else set()
      for volume_dir in sorted(app_dir.iterdir()):
        if not volume_dir.is_dir():
          continue
        volumes.append(
          {
            "app_id": app_id,
            "name": volume_dir.name,
            "kind": kind,
            "path": str(volume_dir),
            "in_use": app_id in running,
            # False means the data outlived whatever declared it: either the
            # app is gone, or its manifest stopped naming this volume.
            "declared": volume_dir.name in declared[app_id],
            "bytes": path_size(volume_dir) if sizes else None,
          }
        )
  return volumes


def host_volumes_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """The `[host_volume]` entries config.toml declares, in tag order."""
  return [
    {
      "tag": tag,
      "path": str(volume.path),
      "readonly": volume.readonly,
      "require_mount": volume.require_mount,
      "exists": volume.path.is_dir(),
    }
    for tag, volume in sorted(ctx.config.host_volumes.items())
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
      # The whole `[app]` table, extras included -- the section allows unknown
      # keys precisely so a happ can carry author, source, license and the
      # like, and a viewer should show whatever the author wrote.
      "metadata": {
        key: value
        for key, value in stack.manifest.app.model_dump().items()
        if value not in (None, "", {})
      },
      "options": {
        "route_providers": sorted(ctx.config.route_providers),
        "host_volumes": sorted(ctx.config.host_volumes),
      },
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
        # As the manifest wrote it, so `${admin_pass}` stays a placeholder.
        # The *resolved* environment is `AppRunData.config_env`, which carries
        # secret values and must never be projected.
        "environment": dict(unit.environment),
        "command": list(unit.command) if unit.command else None,
        "volumes": [
          {
            "name": vol_name,
            "path": bound.guest_path,
            "kind": bound.volume.kind,
            "readonly": bound.readonly,
            "desc": bound.volume.desc,
          }
          for vol_name, bound in unit.volumes.items()
        ],
      }
    )
  return units


def _routes(
  stack: AppStack, run_data: AppRunData, ctx: HarborCtx
) -> list[dict[str, Any]]:
  published = published_route_urls(stack, run_data, ctx)
  assignments = ctx.app_store(stack.app).list_route_assignments()
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
        "provider": assignments.get(name),
      }
    )
  return routes


def _volumes(
  stack: AppStack, run_data: AppRunData, ctx: HarborCtx
) -> list[dict[str, Any]]:
  binds = ctx.app_store(stack.app).list_binds()
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
        # Only `host` volumes are bindable; the rest are harbor's to place.
        "bind": binds.get(name) if volume.kind == "host" else None,
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
