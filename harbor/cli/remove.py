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
      f"Uninstalled {app}. Its data and settings are still here -- reinstall "
      f"it with `harbor install {app}`.\n"
      f"To remove its volumes and configuration too, run "
      f"`harbor uninstall --purge {app}`."
    )
  if plan.mode == RESET:
    return (
      f"Reset {app}. Its settings and address are unchanged; "
      f"start it fresh with `harbor start {app}`."
    )
  return f"Removed {app}"


def _confirmed(plan: RemovalPlan, ctx: HarborCtx, conn: Conn) -> bool:
  app = plan.app_id
  if plan.mode == RESET:
    _describe_reset(plan, conn)
  else:
    conn.out(f"{_DOING[plan.mode]} {app} deletes:")
    for line in _deletes(plan):
      conn.out(f"  {line}")

    conn.out("Left alone:")
    for line in _keeps(plan, ctx):
      conn.out(f"  {line}")

  # No snapshot is taken yet (docs/run-layout.md §8), so say plainly that
  # there is nothing to roll back to rather than implying a safety net that
  # is not there. An uninstall is the exception: everything it takes is
  # rebuilt from the bundle by the next `install`.
  if plan.mode == RESET:
    conn.out("If you want to retain this data, take a snapshot first.")
  elif plan.purges:
    conn.out("If you want this data back, take a snapshot first.")
  try:
    answer = conn.read(f"{_ASKED[plan.mode]} {app}? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")


# What this removal is called mid-sentence, and how it asks.
_DOING = {UNINSTALL: "Uninstalling", RESET: "Resetting", PURGE: "Removing"}
_ASKED = {UNINSTALL: "Uninstall", RESET: "Reset", PURGE: "Remove"}


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


def _deletes(plan: RemovalPlan) -> list[str]:
  lines = []
  if plan.run_path is not None:
    lines.append(f"{plan.run_path} (happ, compose)")
  if plan.config_path is not None:
    lines.append(f"{plan.config_path} (config, secrets)")
  for path in plan.volume_paths:
    # A reset keeps the volume directories themselves; what goes is what the
    # app wrote inside them, which is the part the operator cares about.
    lines.append(f"{path}" if plan.purges else f"everything under {path}")
  if plan.purges:
    lines.append("its route and host-port allocations")
  return lines or ["nothing -- there is no such state on disk"]


def _keeps(plan: RemovalPlan, ctx: HarborCtx) -> list[str]:
  lines = []
  if plan.mode == RESET:
    lines.append("its installation under run/")
  if plan.mode == UNINSTALL:
    lines.append("its data volumes")
  if not plan.purges:
    lines.append("its configuration and secrets")
    lines.append("its route and host-port allocations")

  # Every source that carries the id: a removal deletes the installation,
  # never a bundle, so all of them survive and saying so is the point.
  lines += [str(entry.path) for entry in ctx.app_catalog().get(str(plan.app_id), ())]
  lines += [f"{path} (host volume)" for path in plan.host_paths]
  return lines
