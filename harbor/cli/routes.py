import argparse

from tabulate import tabulate

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.routes import NoopRouteProvider, RouteProviderError, get_route_provider


def register(subparsers) -> None:
  parser = subparsers.add_parser("routes", help="Manage the route provider")
  parser.set_defaults(func=lambda args, ctx, conn: parser.print_help())
  sub = parser.add_subparsers(dest="routes_command")

  check = sub.add_parser("check", help="Validate the configured route provider")
  check.set_defaults(func=run_check)

  add = sub.add_parser("add", help="Register a manual route")
  add.add_argument("subdomain", help="Subdomain under the harbor domain")
  add.add_argument("port", type=int, help="Host port to forward to")
  add.set_defaults(func=run_add)

  remove = sub.add_parser("remove", help="Unregister a manual route")
  remove.add_argument("subdomain", help="Subdomain under the harbor domain")
  remove.set_defaults(func=run_remove)

  list_parser = sub.add_parser("list", help="List registered routes")
  list_parser.set_defaults(func=run_list)


def _provider(ctx: HarborCtx, conn):
  try:
    provider = get_route_provider(ctx.harbor_db(), ctx.config)
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e

  if isinstance(provider, NoopRouteProvider):
    conn.err("No route provider configured")
    raise SystemExit(1)

  return provider


def run_check(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  provider = _provider(ctx, conn)

  errors = provider.validate()
  if errors:
    conn.err("Route provider is not usable:")
    for err in errors:
      conn.err(f"  - {err}")
    raise SystemExit(1)
  conn.out("Route provider OK")


def run_add(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  provider = _provider(ctx, conn)
  domain = ctx.config.domain
  try:
    provider.register_route(AppID("manual"), args.port, args.subdomain, domain)
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e
  conn.out(f"Added {args.subdomain}.{domain} -> :{args.port}")


def run_remove(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  provider = _provider(ctx, conn)
  domain = ctx.config.domain
  try:
    provider.unregister_route(args.subdomain, domain)
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e
  conn.out(f"Removed {args.subdomain}.{domain}")


def run_list(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  provider = _provider(ctx, conn)
  try:
    routes = provider.list_routes()
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e

  if not routes:
    conn.out("No routes")
    return

  domain = ctx.config.domain
  rows = [(f"https://{sub}.{domain}", dest) for sub, dest in routes]
  conn.out(tabulate(rows, headers=["URL", "DESTINATION"], tablefmt="simple"))
