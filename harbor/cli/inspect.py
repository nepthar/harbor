import argparse
from pathlib import Path

from harbor.lib.happ import is_pathlike, load_happ
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
    help="App ID or path to a .happ directory or .happ.md file",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  if is_pathlike(args.app):
    source = Path(args.app).expanduser().resolve()
    stack = load_happ(source).app_stack()
    conn.out(capability_receipt(stack, None, ctx, compact=False))
    return

  app = ctx.resolve_app(args.app)
  # Report what is installed under run/, never the catalog entry under apps/.
  # Pass a path to a .happ to inspect a bundle that is not staged yet.
  stack = app_stack(ctx.app_path(app), app)
  run_data = load_run_data(stack, ctx)
  conn.out(capability_receipt(stack, run_data, ctx, compact=False))
