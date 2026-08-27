"""The three removal verbs, which differ only in how much they take.

An app's state lives in three trees -- the installation under `run/`, its
data under the volume roots, and its config (secrets, routes, host ports)
under `config/`. `uninstall` takes the first, `reset` takes the second, and
`rm` takes all three. See `harbor.lib.lifecycle.rm` for why those are the
combinations on offer and not six switches.
"""

from __future__ import annotations

import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import (
  PURGE,
  RESET,
  UNINSTALL,
  RemovalMode,
  RemovalPlan,
  removal_plan,
  rm,
)
from harbor.lib.util import Conn


def register(subparsers) -> None:
  uninstall = subparsers.add_parser(
    "uninstall",
    help="Uninstall a happ, keeping its data and config unless --purge",
  )
  uninstall.add_argument("app_id", help="App ID of the happ to uninstall")
  uninstall.add_argument(
    "--purge",
    action="store_true",
    help="Also delete its data volumes, config, secrets, and route allocations",
  )
  _add_yes(uninstall)
  uninstall.set_defaults(func=_run(UNINSTALL))

  reset = subparsers.add_parser(
    "reset",
    help="Stop a happ and delete its data, keeping its config and settings",
  )
  reset.add_argument("app_id", help="App ID of the happ to reset")
  _add_yes(reset)
  reset.set_defaults(func=_run(RESET))

  # The old name for `uninstall --purge`, kept because it is the one in
  # every existing note and script.
  remove = subparsers.add_parser(
    "rm",
    help="Alias for `uninstall --purge`",
  )
  remove.add_argument("app_id", help="App ID of the happ to remove")
  _add_yes(remove)
  remove.set_defaults(func=_run(PURGE))


def _add_yes(parser: argparse.ArgumentParser) -> None:
  parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")


def _run(mode: RemovalMode):
  def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
    resolved = PURGE if getattr(args, "purge", False) else mode
    state = ctx.run_state(args.app_id)
    plan = removal_plan(state.app_id, ctx, mode=resolved)

    if not args.yes and not _confirmed(plan, ctx, conn):
      conn.out("Nothing removed.")
      return

    with ctx.locked(f"{plan.mode} {plan.app_id}", plan.app_id):
      rm(plan, ctx)
    conn.out(_done(plan))

  return run


def _done(plan: RemovalPlan) -> str:
  app = plan.app_id
  if plan.mode == UNINSTALL:
    return (
      f"Uninstalled {app}. Configuration and volume data were kept.\n"
      f"To remove those too, run `harbor uninstall --purge {app}`."
    )
  if plan.mode == RESET:
    return (
      f"Reset {app}. Its settings and address are unchanged; "
      f"start it fresh with `harbor start {app}`."
    )
  return f"Removed {app}"


def _confirmed(plan: RemovalPlan, ctx: HarborCtx, conn: Conn) -> bool:
  """Say what the operator is deciding, and nothing else.

  Which directories harbor keeps an app in is its own business: paths appear
  here only for data it is about to destroy, where seeing exactly what is at
  stake is the point of asking at all.
  """
  if plan.mode == RESET:
    _describe_reset(plan, conn)
  elif plan.purges:
    _describe_purge(plan, ctx, conn)
  else:
    conn.out(
      f"Configuration and volume data will be kept. Use "
      f"`harbor uninstall --purge {plan.app_id}` to also remove those."
    )
  try:
    answer = conn.read(f"{_ASKED[plan.mode]} {plan.app_id}? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")


# How each removal asks.
_ASKED = {UNINSTALL: "Uninstall", RESET: "Reset", PURGE: "Remove"}


def _describe_purge(plan: RemovalPlan, ctx: HarborCtx, conn: Conn) -> None:
  """The destructive one, so it names the data going away."""
  volumes = _volume_lines(plan)
  if plan.volume_paths:
    conn.out(f"Removing {plan.app_id} deletes its data volumes:")
    for line in volumes:
      conn.out(f"  {line}")
    conn.out("along with its configuration, secrets, and route allocations.")
  else:
    conn.out(
      f"Removing {plan.app_id} deletes its configuration, secrets, and route "
      f"allocations. It has no data volumes on disk."
    )

  # Host volumes are the operator's own directories, so saying they survive
  # is worth the line -- everything else harbor removes is harbor's.
  for path in plan.host_paths:
    conn.out(f"The host volume at {path} is left alone.")

  # No snapshot is taken yet (docs/run-layout.md §8), so say plainly that
  # there is nothing to roll back to rather than implying a safety net that
  # is not there.
  conn.out("If you want this data back, take a snapshot first.")


def _describe_reset(plan: RemovalPlan, conn: Conn) -> None:
  """A reset is not really a removal, so it does not read like one.

  What the operator is deciding is whether to lose the data; the run dir
  going and coming back is a mechanism, not a consequence.
  """
  conn.out(
    f"Resetting {plan.app_id} will preserve configuration and routing, "
    f"but will remove the following volumes:"
  )
  for line in _volume_lines(plan):
    conn.out(f"  {line}")
  if plan.restage_from is not None:
    conn.out(f"{plan.app_id} is then installed again from {plan.restage_from}.")


def _volume_lines(plan: RemovalPlan) -> list[str]:
  """One line per volume, rather than per volume root, which is how the
  manifest names them and how the operator thinks about them."""
  lines: list[str] = []
  for path in plan.volume_paths:
    volumes = sorted(p for p in path.iterdir() if p.is_dir()) if path.is_dir() else []
    lines += [str(volume) for volume in volumes] or [str(path)]
  return lines or ["nothing -- this app has no data on disk"]
