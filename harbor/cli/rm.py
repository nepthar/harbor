import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import RemovalPlan, removal_plan, rm


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "rm",
    help="Stop a happ and delete its run state, config, managed volumes, and routes",
  )
  parser.add_argument("app_id", help="App ID of the happ to remove")
  parser.add_argument(
    "-y", "--yes", action="store_true", help="Skip confirmation prompt"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  state = ctx.run_state(args.app_id)
  plan = removal_plan(state.app_id, ctx)

  if not args.yes and not _confirmed(plan, ctx, conn):
    conn.out("Nothing removed.")
    return

  rm(plan, ctx)
  conn.out(f"Removed {plan.app_id}")


def _confirmed(plan: RemovalPlan, ctx: HarborCtx, conn) -> bool:
  conn.out(f"Removing {plan.app_id} deletes:")
  conn.out(f"  {plan.run_path} (happ, compose)")
  conn.out(f"  {plan.config_path} (config, secrets)")
  for path in plan.volume_paths:
    conn.out(f"  {path}")
  conn.out("  its route and host-port allocations")

  # Every source that carries the id: `rm` deletes the installation, never a
  # bundle, so all of them survive and saying so is the point of this list.
  kept = [str(entry.path) for entry in ctx.app_catalog().get(str(plan.app_id), ())]
  kept += [f"{path} (host volume)" for path in plan.host_paths]
  conn.out("Left alone:")
  for path in kept:
    conn.out(f"  {path}")

  # No snapshot is taken yet (docs/run-layout.md §8), so say plainly that there
  # is nothing to roll back to rather than implying a safety net that is not
  # there.
  conn.out("If you want to restore this app and your data, take a snapshot first.")
  try:
    answer = conn.read(f"Remove {plan.app_id}? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")
