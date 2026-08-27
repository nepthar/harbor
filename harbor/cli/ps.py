import argparse

from tabulate import tabulate

from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import UNINSTALLED, AppObservation
from harbor.lib.run_layout import load_run_data
from harbor.lib.stack import AppStack
from harbor.lib.views import config_status

EMPTY = "-"


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "ps", help="List installed Harbor apps and their state"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  with ctx.harbor_lock("ps"):
    rows = []
    for observation in ctx.observations():
      if not observation.known:
        continue
      # An uninstalled app has no staged manifest to read, so fall back to
      # the bundle's: what it kept is only legible against the schema it
      # would be reinstalled from. Only then, though -- an installed app
      # whose own manifest will not parse is unknown, not described by the
      # catalog copy it has since diverged from.
      stack = (
        ctx.bundle_stack(observation.app_id)
        if observation.state == UNINSTALLED
        else ctx.staged_stack(observation.app_id)
      )
      rows.append(
        (
          observation.app_id,
          _status(observation),
          _config(observation, stack, ctx),
          _volumes(observation, stack),
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


def _status(observation: AppObservation) -> str:
  if observation.containers:
    return observation.status
  return UNINSTALLED if observation.state == UNINSTALLED else EMPTY


def _config(observation: AppObservation, stack: AppStack | None, ctx: HarborCtx) -> str:
  """Whether this app's settings are complete -- kept config included.

  `harbor uninstall` keeps the config logtab on purpose, so reporting `-`
  here would contradict what it told the operator on the way out.
  """
  if not observation.config_exists:
    return EMPTY
  if observation.state == UNINSTALLED:
    # There is no run dir to check links and ports against, but the question
    # that still has an answer -- are the values the manifest asks for on
    # file -- is the one that decides whether reinstalling needs input.
    if stack is None:
      return "kept"
    return config_status(stack, ctx.app_store(observation.app_id))
  if stack is None:
    # Installed, but its own manifest will not parse: unknown, not
    # unconfigured. Saying "missing" here would send the operator after a
    # config problem they do not have.
    return EMPTY
  return "missing" if load_run_data(stack, ctx).start_blockers else "ready"


def _volumes(observation: AppObservation, stack: AppStack | None) -> str:
  if stack is None:
    return EMPTY
  count = str(len(stack.volumes))
  if observation.state != UNINSTALLED:
    return count
  # Data outlives the installation, so say it is still on disk rather than
  # reporting the manifest's count as though the app were live.
  return f"{count} kept" if observation.volumes_exist else EMPTY
