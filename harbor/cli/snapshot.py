import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import snapshot, start, stop
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
  by = f"snapshot {app}"
  running = 0
  # App lock the whole time; harbor lock only around stop/start so other
  # apps can proceed while volumes copy.
  with ctx.app_lock(app, by):
    with ctx.harbor_lock(by):
      try:
        running = ctx.run_state(app).running_count
      except ValueError:
        running = 0
      if running:
        stop(app, ctx)
    try:
      path = snapshot(app, ctx, label=args.label)
    finally:
      if running:
        with ctx.harbor_lock(by):
          start(app, ctx.config.app_run_path(app), ctx)
  conn.out(f"Snapshot of {app} written to {path}")
