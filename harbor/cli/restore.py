import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import (
  RestorePlan,
  resolve_snapshot_app,
  restore,
  restore_plan,
)
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "restore",
    help="Replace a happ's run state and data volumes with a snapshot's",
  )
  parser.add_argument("app_id", help="App ID of the happ to restore")
  parser.add_argument(
    "snapshot",
    metavar="SNAPSHOT",
    help="Snapshot folder name under snapshots/<app_id>/",
  )
  parser.add_argument(
    "-y", "--yes", action="store_true", help="Skip confirmation prompt"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app = resolve_snapshot_app(ctx, args.app_id)
  plan = restore_plan(app, args.snapshot, ctx)

  if not args.yes and not _confirmed(plan, conn):
    conn.out("Nothing restored.")
    return

  restore(plan, ctx)
  conn.out(f"Restored {plan.app_id} from {plan.snapshot_path}")


def _confirmed(plan: RestorePlan, conn: Conn) -> bool:
  conn.out(f"Restoring {plan.app_id} from {plan.snapshot_path} overwrites:")
  conn.out(f"  {plan.run_path} (config, secrets, happ, compose)")
  for _, dest in plan.data_volumes:
    conn.out(f"  {dest}")
  conn.out("  its route and host-port allocations")

  # No branching, no history: whatever is there now is simply gone, and the
  # snapshot's state becomes the current state from here on.
  conn.out(
    f"Whatever {plan.app_id} holds right now is destroyed, not set aside. "
    f"Snapshot it first if you want to keep it."
  )
  try:
    answer = conn.read(f"Restore {plan.app_id} to {plan.snapshot_path.name}? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")
