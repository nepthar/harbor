import argparse
from pathlib import Path

from harbor.lib.apps import is_pathlike
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import capability_receipt
from harbor.lib.run_layout import load_run_data
from harbor.lib.stack import app_stack


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "inspect",
    help="Show images, ports, routes, volumes, and sharp edges for a happ",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID or path to a .happ directory",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  if is_pathlike(args.app):
    source = Path(args.app).expanduser().resolve()
    conn.out(capability_receipt(app_stack(source), None, ctx, compact=False))
    return

  app = ctx.resolve_app(args.app)
  if ctx.is_staged(app):
    # Report what is installed, not what the catalog entry says today.
    stack = app_stack(ctx.app_path(app), app)
    run_data = load_run_data(stack, ctx)
  else:
    stack = app_stack(ctx.bundle_path(app), app)
    run_data = None
  conn.out(capability_receipt(stack, run_data, ctx, compact=False))
