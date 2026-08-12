import argparse

from harbor.cli.kv import parse_kv
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import staging_target, start
from harbor.lib.receipt import capability_receipt, location_receipt
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "start",
    help="Start a happ, staging it first if needed (accepts app id or .happ path)",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID (e.g. io.example.myapp or myapp) or path to a .happ directory or .happ.md file",
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
    metavar="VOLUME=HOST_VOLUME",
    help="Bind an app volume to a host_volume tag before starting (repeatable)",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  target = staging_target(ctx, args.app)
  if target.linked_entry is not None:
    conn.out(f"Linked {target.linked_entry} -> {target.linked_entry.resolve()}")

  sets = [parse_kv(item, "--set") for item in args.sets]
  binds = [parse_kv(item, "--bind") for item in args.binds]
  if target.bundle is not None:
    bundle = target.bundle
  elif sets or binds or not ctx.is_staged(target.app_id):
    bundle = ctx.bundle_path(target.app_id)
  else:
    # Catalog may be gone; start will use the run copy as-is.
    bundle = ctx.config.app_run_path(target.app_id)
  result = start(target.app_id, bundle, ctx, sets=sets, binds=binds)

  compact = capability_receipt(result.stack, result.run_data, ctx, compact=True)
  if compact.strip():
    conn.out(compact)
  conn.out(location_receipt(result.stack, result.run_data, ctx))
