"""JSON projections of harbor state.

The one rule here: **no secret ever leaves this module.** `AppRunData` carries
resolved config values, secrets included, because compose needs them; what a
viewer gets is a non-secret value or the fact that a secret is set. Shapes are
shared by the daemon's HTTP API and the CLI's `--json` output.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from typing import Any

from harbor.lib import activity
from harbor.lib.apps import AppID
from harbor.lib.happ import load_happ, manifest_text
from harbor.lib.harbor import CatalogEntry, HarborCtx
from harbor.lib.lifecycle.restore import snapshot_names, snapshotted_app_ids
from harbor.lib.lifecycle.snapshot import snapshot_archive, split_snapshot_name
from harbor.lib.observations import AppObservation
from harbor.lib.receipt import published_route_urls
from harbor.lib.repo import MAIN_REPO, bound_apps
from harbor.lib.run_layout import AppRunData, load_run_data
from harbor.lib.stack import AppStack
from harbor.lib.store import AppStore
from harbor.lib.util import path_size


def apps_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every installed app, in the shape a dashboard list wants.

  Uninstalled apps belong to the catalog listing, which reports their state.
  """
  return [
    _summary(observation, ctx)
    for observation in ctx.observations()
    if observation.installed
  ]


def metrics_view(ctx: HarborCtx, prefix: str, hours: int) -> dict[str, Any]:
  """Gauge history for keys starting `gauge/{prefix}` over the last `hours` hours."""
  until = int(datetime.now(UTC).timestamp())
  since = until - hours * 60 * 60
  metrics: dict[str, list[dict[str, int | float]]] = {}
  for key, entries in ctx.history_gauges(prefix, since).items():
    metrics[key.removeprefix("gauge/")] = [
      {"t": e.unix_seconds, "v": float(e.value)} for e in entries
    ]
  return {"since": since, "until": until, "metrics": metrics}


def catalog_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every configured repo, with the happs currently in it."""
  catalogs: dict[str, list[dict[str, Any]]] = {name: [] for name in ctx.config.repos}
  catalog = ctx.app_catalog()
  for app_id in sorted(catalog):
    for entry in catalog[app_id]:
      catalogs.setdefault(entry.source, []).append(_catalog_app(entry, ctx))
  return [{"name": name, "apps": apps} for name, apps in catalogs.items()]


def _catalog_app(entry: CatalogEntry, ctx: HarborCtx) -> dict[str, Any]:
  stack = _catalog_stack(entry)
  # The logtab is what makes an id more than a catalog listing, and AppStore
  # creates it on contact -- so this is a file check, never a store lookup.
  has_config = ctx.config.app_config_path(entry.app_id).is_file()
  store = ctx.app_store(stack.app) if stack is not None and has_config else None
  manifest = manifest_text(entry.path)
  return {
    "app_id": entry.app_id,
    "display_name": stack.display_name if stack else "",
    "version": stack.version if stack else None,
    "description": stack.description if stack else "",
    "repo": entry.source,
    "state": ctx.app_state(entry.app_id),
    "configured": config_status(stack, store) if stack else None,
    "manifest": manifest,
    "manifest_stale": _catalog_manifest_stale(entry, manifest, ctx),
  }


def _manifest_diff(current: str, remote: str) -> str:
  return "".join(
    difflib.unified_diff(
      current.splitlines(keepends=True),
      remote.splitlines(keepends=True),
      fromfile="installed",
      tofile="remote",
    )
  )


def _catalog_manifest_stale(entry: CatalogEntry, manifest: str, ctx: HarborCtx) -> bool:
  """Whether the catalog's manifest has moved on from the staged copy."""
  staged = ctx.staged_paths(entry.app_id).manifest_path
  if not manifest or not staged.is_file():
    return False
  try:
    return staged.read_text() != manifest
  except OSError:
    return False


def config_status(stack: AppStack, store: AppStore | None) -> str:
  """ "ready" when every config key is set or will be filled, else "missing"."""
  for name, cfg in stack.config.items():
    if cfg.default is not None:
      continue
    if store is not None and store.has_config(name):
      continue
    return "missing"
  return "ready"


def repos_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every configured repo: what it is, and what it last mirrored."""
  catalog = ctx.app_catalog()
  out = []
  for name, repo in ctx.config.repos.items():
    state = ctx.harbor_db.get_repo_state(name) if repo.mirrored else None
    out.append(
      {
        "name": name,
        "kind": repo.kind,
        "location": repo.describe(),
        "url": repo.remote.url if repo.remote else None,
        "path": str(repo.path),
        "exists": repo.path.is_dir(),
        "removable": name != MAIN_REPO,
        "apps": sum(
          1 for entries in catalog.values() for e in entries if e.source == name
        ),
        "bound_apps": list(bound_apps(ctx, name)),
        "sha": state["sha"] if state else None,
        "updated_at": state["at"] if state else None,
      }
    )
  return out


def contested_view(ctx: HarborCtx) -> dict[str, list[str]]:
  """App ids more than one repo carries, and which repos those are."""
  return {app_id: sorted(repos) for app_id, repos in ctx.contested_app_ids().items()}


def _catalog_stack(entry: CatalogEntry) -> AppStack | None:
  """The bundle's schema, or None when the happ on disk does not parse."""
  try:
    return load_happ(entry.path).app_stack()
  except (ValueError, RuntimeError):
    return None


def _gauge_bytes(gauges: dict[str, Any], name: str) -> int | None:
  entry = gauges.get("gauge/" + name)
  if entry is None:
    return None
  try:
    return int(float(entry.value))
  except ValueError:
    return None


def volumes_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every harbor-managed volume on disk, whatever declared it."""
  running = {
    observation.app_id
    for observation in ctx.observations()
    if observation.running_count
  }
  declared: dict[str, set[str]] = {}
  volumes = []
  gauges = ctx.read_gauges("volume_size_bytes/")

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
            "bytes": _gauge_bytes(
              gauges, f"volume_size_bytes/{app_id}/{kind}/{volume_dir.name}"
            ),
          }
        )
  return volumes


def host_volumes_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """The `[host_volume]` entries config.toml declares, in tag order."""
  gauges = ctx.read_gauges("volume_size_bytes//host/")
  return [
    {
      "tag": tag,
      "path": str(volume.path),
      "readonly": volume.readonly,
      "require_mount": volume.require_mount,
      "exists": volume.path.is_dir(),
      "bytes": _gauge_bytes(gauges, f"volume_size_bytes//host/{tag}"),
    }
    for tag, volume in sorted(ctx.config.host_volumes.items())
  ]


# The directories `record_volume_sizes` gauges by name, in display order.
HARBOR_DIRS = ("var", "snapshots", "repos")


def harbor_dir_sizes(ctx: HarborCtx) -> dict[str, int | None]:
  """Harbor's own directories, as last recorded by volume-metrics."""
  return {
    f"{name}_bytes": _gauge_bytes(
      ctx.read_gauges(f"{name}_size_bytes"), f"{name}_size_bytes"
    )
    for name in HARBOR_DIRS
  }


def snapshots_view(ctx: HarborCtx) -> list[dict[str, Any]]:
  """Every snapshot archive, newest name first."""
  rows = []
  for app_id in snapshotted_app_ids(ctx):
    for name in snapshot_names(app_id, ctx):
      taken_at, tag = split_snapshot_name(name)
      archive = snapshot_archive(ctx.config.snapshot_root, app_id, name)
      rows.append(
        {
          "app_id": str(app_id),
          "name": name,
          "taken_at": taken_at,
          "tag": tag,
          "bytes": archive.stat().st_size if archive.is_file() else None,
        }
      )
  rows.sort(key=lambda row: row["name"], reverse=True)
  return rows


def activity_view(
  ctx: HarborCtx, *, app: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
  """Recorded unattended runs, newest first; see `lib/activity.py`."""
  return activity.list_runs(ctx, app=app, limit=limit)


def activity_log_view(ctx: HarborCtx, filename: str) -> dict[str, Any]:
  """One run's output file, as `list_runs` named it in `log`."""
  text = activity.read_run_log(ctx, filename)
  middle = filename.removesuffix(".log").split(".")[1:-1]
  return {
    "app_id": ".".join(middle) or None,
    "file": filename,
    "text": text,
  }


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
    "state": observation.state,
    "containers": {
      "running": observation.running_count,
      "total": len(observation.containers),
    },
    "configured": _configured(observation, stack, ctx),
    "config_pending": observation.config_pending,
    "volume_count": len(stack.volumes) if stack else 0,
    "last_action": observation.last_action,
  }


def _configured(
  observation: AppObservation, stack: AppStack | None, ctx: HarborCtx
) -> str | None:
  """ "ready", "missing", or None when the app has no config store yet."""
  if not observation.config_exists or stack is None:
    return None
  return "missing" if load_run_data(stack, ctx).start_blockers else "ready"


def _units(stack: AppStack, observation: AppObservation) -> list[dict[str, Any]]:
  """Declared run units joined to whatever containers are actually up."""
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
        "advanced": config.advanced,
        "set": store.has_config(name),
        "has_default": config.has_default(),
        # A secret's value is never projected -- not even when it is set, and
        # not even to say how long it is.
        "value": None if config.secret or value is None else value.value,
      }
    )
  return entries
