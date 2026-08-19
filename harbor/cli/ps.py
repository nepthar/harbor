import argparse

from tabulate import tabulate

from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import AppObservation
from harbor.lib.run_layout import load_run_data
from harbor.lib.stack import AppStack

EMPTY = "-"


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "ps", help="List installed Harbor apps and their state"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  rows = []
  for observation in ctx.observations():
    if not observation.installed:
      continue
    stack = ctx.staged_stack(observation.app_id)
    rows.append(
      (
        observation.app_id,
        observation.status if observation.containers else EMPTY,
        _config(observation, stack, ctx),
        _volumes(stack),
        observation.last_action or EMPTY,
      )
    )
  conn.out(
    tabulate(
      rows,
      headers=["APP_ID", "STATUS", "CONFIG", "VOLUMES", "LAST_ACTION"],
      tablefmt="simple",
    )
  )


def _config(observation: AppObservation, stack: AppStack | None, ctx: HarborCtx) -> str:
  if not observation.config_exists:
    return EMPTY
  if stack is None:
    return EMPTY
  return "missing" if load_run_data(stack, ctx).start_blockers else "ready"


def _volumes(stack: AppStack | None) -> str:
  if stack is None:
    return EMPTY
  return str(len(stack.volumes))
