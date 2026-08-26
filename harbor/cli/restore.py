import argparse

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import (
  RestorePlan,
  resolve_snapshot_app,
  restore,
  restore_plan,
  snapshot_names,
)
from harbor.lib.util import Conn

# How many snapshot names to show when SNAPSHOT is omitted.
_RECENT_SNAPSHOT_LIMIT = 10


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "restore",
    help="Replace a happ's run state and data volumes with a snapshot's",
  )
  parser.add_argument("app_id", help="App ID of the happ to restore")
  parser.add_argument(
    "snapshot",
    metavar="SNAPSHOT",
    nargs="?",
    help="Snapshot name under snapshots/<app_id>/",
  )
  parser.add_argument(
    "-y", "--yes", action="store_true", help="Skip confirmation prompt"
  )
  parser.add_argument(
    "--no-snapshot",
    action="store_true",
    help="Do not take a pre-restore snapshot of the current run dir",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app = resolve_snapshot_app(ctx, args.app_id)
  if not args.snapshot:
    raise ValueError(_missing_snapshot_message(app, ctx))
  plan = restore_plan(app, args.snapshot, ctx)
  snapshot_first = not args.no_snapshot

  if not args.yes and not _confirmed(plan, snapshot_first, conn):
    conn.out("Nothing restored.")
    return

  with ctx.locked(f"restore {app}", app):
    restore(plan, ctx, snapshot_first=snapshot_first)
  conn.out(f"Restored {plan.app_id} from {plan.snapshot_path}")


def _missing_snapshot_message(app: AppID, ctx: HarborCtx) -> str:
  # snapshot_names is oldest-first; newest-first is what the operator wants
  # when picking which one to restore.
  recent = list(reversed(snapshot_names(app, ctx)[-_RECENT_SNAPSHOT_LIMIT:]))
  detail = "\n".join(f"  {name}" for name in recent) if recent else "  (none)"
  return (
    f"SNAPSHOT is required. Recent snapshots for {app}:\n{detail}\n"
    f"Restore with `harbor restore {app} <snapshot>`"
  )


def _confirmed(plan: RestorePlan, snapshot_first: bool, conn: Conn) -> bool:
  conn.out(f"Restoring {plan.app_id} from {plan.snapshot_path} overwrites:")
  conn.out(f"  {plan.run_path} (happ, compose)")
  conn.out(f"  {plan.config_path} (config, secrets)")
  for _, dest in plan.data_volumes:
    conn.out(f"  {dest}")
  conn.out("  its route and host-port allocations")

  if plan.run_path.exists() and not snapshot_first:
    conn.out(
      f"Whatever {plan.app_id} holds right now is destroyed, not set aside "
      f"(--no-snapshot)."
    )
  if snapshot_first and plan.run_path.exists() and not plan.is_latest_pre_restore:
    prompt = (
      f"Snapshot {plan.app_id} first, then restore to {plan.snapshot_path.name}? [y/N] "
    )
  else:
    if snapshot_first and plan.run_path.exists():
      conn.out(
        "This is the newest pre-restore snapshot; no new pre-restore "
        "snapshot will be taken."
      )
    prompt = f"Restore {plan.app_id} to {plan.snapshot_path.name}? [y/N] "
  try:
    answer = conn.read(prompt)
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")
