import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import reload_app, staging_target
from harbor.lib.receipt import capability_receipt
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "reload",
    help="Re-install a happ from its bundle, restarting it if it was running",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID (e.g. io.example.myapp or myapp) or path to an app",
  )
  parser.add_argument(
    "--force",
    action="store_true",
    help="Reload even though this id was last installed from somewhere else",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  target = staging_target(ctx, args.app, force=args.force)
  app = target.app_id
  with ctx.locked(f"reload {app}", app):
    result = reload_app(
      app, target.bundle or ctx.bundle_path(app), ctx, bound=target.bound_to
    )

  for name in result.stage.dropped_volumes:
    conn.err(
      f"volume {name} is no longer declared in the manifest; "
      f"its link is gone but its data was left in place"
    )

  stage = result.stage
  if result.was_running:
    conn.out(f"Reloaded {app}")
    conn.out(capability_receipt(stage.stack, stage.run_data, ctx, compact=True))
  else:
    # Not running before, so not running now: say so rather than let the
    # absence of an error read as "it came back up".
    conn.out(f"Re-installed {app}; it was not running, so it was not started.")
    conn.out(f"Start it with: harbor start {app}")
