import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import snapshot
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
    help="Optional label appended to the snapshot folder name",
  )
  # Default holds_lock=True: the whole copy (including sudo volume cp) runs under
  # the harbor lock so nothing else mutates the app mid-snapshot.
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app = ctx.resolve_app(args.app)
  path = snapshot(app, ctx, label=args.label)
  conn.out(f"Snapshot of {app} written to {path}")
