from __future__ import annotations

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle._common import logger
from harbor.lib.routes import (
  RouteProviderError,
  get_route_provider,
  refuse_foreign_route,
)
from harbor.lib.run_layout import AppRunData, AssignedRoute


def web_routes(run_data: AppRunData) -> list[tuple[str, AssignedRoute]]:
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
  routes = web_routes(run_data)
  if not routes:
    return

  provider = get_route_provider(ctx.harbor_db(), ctx.config)
  owners = provider.route_owners()
  domain = ctx.config.domain
  for _, route in routes:
    subdomain = route.subdomain
    if subdomain not in owners:
      continue
    owner = owners[subdomain]
    if owner == run_data.app:
      continue
    raise refuse_foreign_route(f"{subdomain}.{domain}", owner)


def register_app_routes(run_data: AppRunData, ctx: HarborCtx) -> None:
  routes = web_routes(run_data)
  if not routes:
    return

  provider = get_route_provider(ctx.harbor_db(), ctx.config)
  domain = ctx.config.domain
  for route_name, route in routes:
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
