import argparse

from harbor.lib.apps import read_last_app_action
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import status_receipt
from harbor.lib.run_layout import load_run_data
from harbor.lib.stack import app_stack


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "status", help="Show detailed status and reachability for an installed happ"
  )
  parser.add_argument("app_id", help="App ID of the happ")
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  app = ctx.resolve_app(args.app_id)
  source = ctx.app_path(app)
  stack = app_stack(source, app)
  run_data = load_run_data(stack, ctx)
  state = ctx.run_state(app)
  total = len(state.containers)
  if state.running_count:
    state_line = (
      f"running, {state.running_count}/{total or state.running_count} containers"
    )
  elif total:
    state_line = f"exited, 0/{total} containers"
  elif not state.compose_exists:
    state_line = "broken" if state.run_dir_exists else "not installed"
  elif run_data.start_blockers:
    state_line = "needs config"
  else:
    state_line = "exited"

  last_action = read_last_app_action(app, ctx.config)

  conn.out(
    status_receipt(
      stack,
      run_data,
      ctx,
      state_line=state_line,
      source=source,
      last_action=last_action,
    )
  )
