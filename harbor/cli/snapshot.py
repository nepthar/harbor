import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import take_snapshot
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "snapshot",
    help="Capture a restore point for a staged happ (config, happ, and data volumes)",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID of the happ to snapshot",
  )
  parser.add_argument(
    "--label",
    default="",
    metavar="LABEL",
    help="Optional label appended to the snapshot name",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app = ctx.resolve_app(args.app)
  conn.out(f"Snapshotting {app}...")
  path = take_snapshot(app, ctx, label=args.label)
  conn.out(f"Snapshot of {app} written to {path}")
