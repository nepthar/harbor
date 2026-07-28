import argparse

from tabulate import tabulate

from harbor.lib.harbor import HarborCtx


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "catalog", help="List available happ bundles under apps/"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  bundles = ctx.known_bundles()
  staged = ctx._staged_sources()
  rows = []
  for app_id in sorted(bundles):
    path = bundles[app_id]
    source = "installed" if app_id in staged else "apps"
    rows.append((app_id, source, str(path)))
  conn.out(tabulate(rows, headers=["APP_ID", "SOURCE", "PATH"], tablefmt="simple"))
