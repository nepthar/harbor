import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stop


def register(subparsers) -> None:
  parser = subparsers.add_parser("down", help="Stop a running happ")
  parser.add_argument("app_id", help="App ID of the happ to stop")
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  state = ctx.run_state(args.app_id)
  stop(state.app_id, ctx)
