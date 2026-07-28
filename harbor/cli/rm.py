import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import reset, unstage


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "rm",
    help="Remove an app's runtime and/or data (requires --runtime and/or --data)",
  )
  parser.add_argument("app_id", help="App ID of the happ to remove")
  parser.add_argument(
    "--runtime",
    action="store_true",
    help="Remove run state only (preserves volumes and config)",
  )
  parser.add_argument(
    "--data",
    action="store_true",
    help="Delete managed volumes, config, and run state",
  )
  parser.add_argument(
    "-y", "--yes", action="store_true", help="Skip confirmation prompt"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  if not args.runtime and not args.data:
    raise ValueError(
      "Specify --runtime (remove run state) and/or --data (delete volumes and config)"
    )

  state = ctx.run_state(args.app_id)

  if args.data:
    if not args.yes:
      answer = conn.read(
        f"Remove {args.app_id}? This deletes all data and config. [y/N] "
      )
      if answer.lower() not in ("y", "yes"):
        return
    reset(state.app_id, ctx)
    conn.out(f"Removed {state.app_id} (runtime and data)")
    return

  unstage(state.app_id, ctx)
  conn.out(f"Removed runtime for {state.app_id}")
