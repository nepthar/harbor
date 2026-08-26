from __future__ import annotations

from harbor.lib.apps import AppID
from harbor.lib.config import NONE_ROUTE_PROVIDER_TAG
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle._common import logger
from harbor.lib.routes import (
  RouteProviderError,
  get_route_provider,
  refuse_foreign_route,
)
from harbor.lib.run_layout import AppRunData, AssignedRoute


def assigned_routes(
  run_data: AppRunData, ctx: HarborCtx
) -> list[tuple[str, AssignedRoute, str]]:
  """Routes with a non-none provider assignment: (name, route, provider_tag)."""
  assignments = ctx.app_store(run_data.app).list_route_assignments()
  out: list[tuple[str, AssignedRoute, str]] = []
  for route_name, route in run_data.routes.items():
    tag = assignments.get(route_name)
    if not tag or tag == NONE_ROUTE_PROVIDER_TAG:
      logger.debug(
        "route %s has no provider assignment (or none); skipping", route_name
      )
      continue
    out.append((route_name, route, tag))
  return out


def preflight_app_routes(run_data: AppRunData, ctx: HarborCtx) -> None:
  """Sanity check that assigned routes can be satisfied by their providers.

  If two apps request the same subdomain on the same provider, the first app
  started wins, and the second fails here.
  """
  routes = assigned_routes(run_data, ctx)
  if not routes:
    return

  for _, route, tag in routes:
    provider = get_route_provider(ctx, tag)
    owners = provider.route_owners()
    subdomain = route.subdomain
    if subdomain not in owners:
      continue
    owner = owners[subdomain]
    if owner == run_data.app:
      continue
    domain = ctx.config.provider_domain(tag)
    raise refuse_foreign_route(f"{subdomain}.{domain}", owner)


def register_app_routes(run_data: AppRunData, ctx: HarborCtx) -> None:
  routes = assigned_routes(run_data, ctx)
  if not routes:
    return

  for route_name, route, tag in routes:
    host_port = run_data.routes[route_name].host_port
    if host_port < 0:
      raise RouteProviderError(
        f"route {route_name!r} has no allocated host port; run `harbor stage` first"
      )

    provider = get_route_provider(ctx, tag)
    domain = ctx.config.provider_domain(tag)
    provider.register_route(
      run_data.app, host_port, route.subdomain, domain, scheme=route.scheme
    )
    logger.info(
      "registered route %s via %s: %s.%s -> %s://:%d",
      route_name,
      tag,
      route.subdomain,
      domain,
      route.scheme,
      host_port,
    )


def unregister_app_routes(app: AppID, ctx: HarborCtx) -> None:
  hdb_routes = ctx.harbor_db.list_routes(app)
  assignments = ctx.app_store(app).list_route_assignments()
  for route_name, route_dict in hdb_routes.items():
    tag = assignments.get(route_name)
    if not tag or tag == NONE_ROUTE_PROVIDER_TAG:
      continue
    try:
      route = AssignedRoute(
        name=route_dict["name"],
        subdomain=route_dict["subdomain"],
        run_unit_name=route_dict["run_unit_name"],
        host_port=route_dict["host_port"],
        container_port=route_dict["container_port"],
        proto=route_dict["proto"],
        scheme=route_dict["scheme"],
      )
    except (KeyError, TypeError) as e:
      logger.error("skipping malformed route record %s for %s: %s", route_name, app, e)
      continue

    provider = get_route_provider(ctx, tag)
    domain = ctx.config.provider_domain(tag)
    try:
      provider.unregister_route(route.subdomain, domain)
      logger.info(
        "unregistered route %s via %s: %s.%s",
        route.name,
        tag,
        route.subdomain,
        domain,
      )
    except RouteProviderError as e:
      logger.error(
        "failed to unregister route %s for %s: %s",
        route.name,
        app,
        e,
      )


def sync_route_assignment(
  app: AppID,
  route_name: str,
  old_tag: str | None,
  new_tag: str,
  ctx: HarborCtx,
) -> None:
  """Push an assignment change to the old/new providers.

  The AppStore assignment is already written by the caller; this only talks to
  providers, which need the allocated host port from harbordb.
  """
  hdb_routes = ctx.harbor_db.list_routes(app)
  entry = hdb_routes.get(route_name)
  if entry is None:
    raise ValueError(
      f"route {route_name!r} has no allocated host port for {app}; "
      f"run `harbor stage {app}` first"
    )

  subdomain = entry["subdomain"]
  host_port = entry["host_port"]
  scheme = entry.get("scheme", "http")

  if old_tag and old_tag != NONE_ROUTE_PROVIDER_TAG:
    try:
      old = get_route_provider(ctx, old_tag)
      old.unregister_route(subdomain, ctx.config.provider_domain(old_tag))
    except RouteProviderError as e:
      logger.error("failed to unregister %s from %s: %s", route_name, old_tag, e)

  if new_tag != NONE_ROUTE_PROVIDER_TAG:
    if host_port < 0:
      raise ValueError(
        f"route {route_name!r} has no allocated host port for {app}; "
        f"run `harbor stage {app}` first"
      )
    new = get_route_provider(ctx, new_tag)
    new.register_route(
      app, host_port, subdomain, ctx.config.provider_domain(new_tag), scheme=scheme
    )
