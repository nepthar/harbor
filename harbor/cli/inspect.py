import argparse

from harbor.lib.apps import resolve_app_or_path
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
  app, source = resolve_app_or_path(ctx, args.app)
  stack = app_stack(source)
  run_data = None
  try:
    if ctx.run_path(app).is_dir():
      run_data = load_run_data(stack, ctx, app_path=source)
  except ValueError:
    run_data = None
  conn.out(capability_receipt(stack, run_data, ctx, compact=False))
