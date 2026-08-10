import argparse

from tabulate import tabulate

from harbor.lib.apps import AppID
from harbor.lib.config import NONE_ROUTE_PROVIDER_TAG
from harbor.lib.harbor import HarborCtx
from harbor.lib.routes import NoopRouteProvider, RouteProviderError, get_route_provider


def register(subparsers) -> None:
  parser = subparsers.add_parser("routes", help="Manage route providers")
  parser.set_defaults(func=lambda args, ctx, conn: parser.print_help())
  sub = parser.add_subparsers(dest="routes_command")

  check = sub.add_parser("check", help="Validate a configured route provider")
  check.add_argument(
    "provider",
    nargs="?",
    help="Provider tag (default: default_route_provider)",
  )
  check.set_defaults(func=run_check)

  add = sub.add_parser("add", help="Register a manual route")
  add.add_argument("subdomain", help="Subdomain under the provider domain")
  add.add_argument("port", type=int, help="Host port to forward to")
  add.add_argument(
    "--provider",
    "-p",
    dest="provider",
    help="Provider tag (default: default_route_provider)",
  )
  add.set_defaults(func=run_add)

  remove = sub.add_parser("remove", help="Unregister a manual route")
  remove.add_argument("subdomain", help="Subdomain under the provider domain")
  remove.add_argument(
    "--provider",
    "-p",
    dest="provider",
    help="Provider tag (default: default_route_provider)",
  )
  remove.set_defaults(func=run_remove)

  list_parser = sub.add_parser("list", help="List registered routes")
  list_parser.add_argument(
    "provider",
    nargs="?",
    help="Provider tag (default: default_route_provider)",
  )
  list_parser.set_defaults(func=run_list)


def _resolve_tag(ctx: HarborCtx, tag: str | None) -> str:
  resolved = tag or ctx.config.default_route_provider
  if resolved == NONE_ROUTE_PROVIDER_TAG:
    raise ValueError(
      f"No route provider selected (default is {NONE_ROUTE_PROVIDER_TAG!r}); "
      f"pass a provider tag or set default_route_provider in config.toml"
    )
  if resolved not in ctx.config.route_providers:
    known = ", ".join(sorted(ctx.config.route_providers))
    raise ValueError(f"No route provider tagged {resolved!r}; known tags: {known}")
  return resolved


def _provider(ctx: HarborCtx, conn, tag: str | None):
  try:
    resolved = _resolve_tag(ctx, tag)
  except ValueError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e

  try:
    provider = get_route_provider(ctx.harbor_db(), ctx.config, resolved)
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e

  if isinstance(provider, NoopRouteProvider):
    conn.err(f"Route provider {resolved!r} is a noop provider")
    raise SystemExit(1)

  return resolved, provider


def run_check(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  tag, provider = _provider(ctx, conn, args.provider)

  errors = provider.validate()
  if errors:
    conn.err(f"Route provider {tag!r} is not usable:")
    for err in errors:
      conn.err(f"  - {err}")
    raise SystemExit(1)
  conn.out(f"Route provider {tag!r} OK")


def run_add(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  tag, provider = _provider(ctx, conn, args.provider)
  domain = ctx.config.provider_domain(tag)
  try:
    provider.register_route(AppID("manual"), args.port, args.subdomain, domain)
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e
  conn.out(f"Added {args.subdomain}.{domain} -> :{args.port} via {tag}")


def run_remove(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  tag, provider = _provider(ctx, conn, args.provider)
  domain = ctx.config.provider_domain(tag)
  try:
    provider.unregister_route(args.subdomain, domain)
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e
  conn.out(f"Removed {args.subdomain}.{domain} via {tag}")


def run_list(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  tag, provider = _provider(ctx, conn, args.provider)
  try:
    routes = provider.list_routes()
  except RouteProviderError as e:
    conn.err(f"Error: {e}")
    raise SystemExit(1) from e

  if not routes:
    conn.out("No routes")
    return

  domain = ctx.config.provider_domain(tag)
  rows = [(f"https://{sub}.{domain}", dest) for sub, dest in routes]
  conn.out(tabulate(rows, headers=["URL", "DESTINATION"], tablefmt="simple"))
