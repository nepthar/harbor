import argparse
from pathlib import Path

from harbor.lib.happ import app_id_from_path, is_pathlike
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
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app = (
    app_id_from_path(Path(args.app).expanduser().resolve())
    if is_pathlike(args.app)
    else ctx.resolve_app(args.app)
  )
  with ctx.locked(f"stage {app}", app):
    target = staging_target(ctx, args.app)
    app = target.app_id
    if target.linked_entry is not None:
      conn.out(f"Linked {target.linked_entry} -> {target.linked_entry.resolve()}")

    result = stage(app, target.bundle or ctx.bundle_path(app), ctx)
    for name in result.dropped_volumes:
      conn.err(
        f"volume {name} is no longer declared in the manifest; "
        f"its link is gone but its data was left in place"
      )
    conn.out(f"Installed {app} at {ctx.run_path(app)}")
    conn.out(f"Start it with: harbor start {app}")
