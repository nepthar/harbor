import argparse

from tabulate import tabulate

from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import AppObservation
from harbor.lib.run_layout import load_run_data
from harbor.lib.stack import app_stack


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "ps", help="List installed Harbor apps and their state"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  rows = []
  for observation in ctx.observations():
    if not _is_installed(observation):
      continue
    rows.append(
      (
        observation.app_id,
        _status(observation, ctx),
        observation.last_action or "-",
      )
    )
  conn.out(
    tabulate(
      rows,
      headers=["APP_ID", "STATUS", "LAST_ACTION"],
      tablefmt="simple",
    )
  )


def _is_installed(observation: AppObservation) -> bool:
  return bool(
    observation.run_dir_exists or observation.containers or observation.db_present
  )


def _status(observation: AppObservation, ctx: HarborCtx) -> str:
  if observation.running_count:
    return "running"
  if observation.containers:
    return "exited"
  if not observation.compose_exists:
    return "broken" if observation.run_dir_exists else "orphaned"

  try:
    stack = app_stack(ctx.app_path(observation.app_id), observation.app_id)
  except ValueError:
    # A missing or unparseable staged manifest -- the app could not be loaded
    # at all, which is not the same as it needing configuration.
    return "unreadable"
  return "needs config" if load_run_data(stack, ctx).start_blockers else "exited"
