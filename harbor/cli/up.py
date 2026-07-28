import argparse

from harbor.cli.kv import parse_kv
from harbor.lib.apps import resolve_app_or_path
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import up
from harbor.lib.receipt import capability_receipt, location_receipt
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "up",
    help="Materialize and start a happ (accepts app id or .happ path)",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID (e.g. io.example.myapp or myapp) or path to a .happ directory",
  )
  parser.add_argument(
    "--set",
    action="append",
    default=[],
    dest="sets",
    metavar="KEY=VALUE",
    help="Set a config value before starting (repeatable)",
  )
  parser.add_argument(
    "--bind",
    action="append",
    default=[],
    dest="binds",
    metavar="VOLUME=HOST_PATH",
    help="Bind an external volume before starting (repeatable)",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app, source = resolve_app_or_path(ctx, args.app)
  sets = [parse_kv(item, "--set") for item in args.sets]
  binds = [parse_kv(item, "--bind") for item in args.binds]
  result = up(app, ctx, source, sets=sets, binds=binds)
  compact = capability_receipt(result.stack, result.run_data, ctx, compact=True)
  if compact.strip():
    conn.out(compact)
  conn.out(location_receipt(result.stack, result.run_data, ctx))
