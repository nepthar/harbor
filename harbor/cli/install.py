import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stage, staging_target
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "install",
    help="Install a fetched happ so it can be started (accepts app id or .happ path)",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID (e.g. io.example.myapp or myapp) or path to an app",
  )
  parser.add_argument(
    "--force",
    action="store_true",
    help="Install even though this id was last installed from somewhere else",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  target = staging_target(ctx, args.app, force=args.force)
  app = target.app_id
  with ctx.locked(f"stage {app}", app):
    result = stage(
      app, target.bundle or ctx.bundle_path(app), ctx, bound=target.bound_to
    )
    for name in result.dropped_volumes:
      conn.err(
        f"volume {name} is no longer declared in the manifest; "
        f"its link is gone but its data was left in place"
      )
    conn.out(f"Installed {app} at {ctx.run_path(app)}")
    conn.out(f"Start it with: harbor start {app}")
