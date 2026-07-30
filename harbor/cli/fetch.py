import argparse

from harbor.lib.fetch import (
  USAGE,
  commit_happ,
  destination_for,
  discard,
  parse_target,
  stage_happ,
)
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import capability_receipt
from harbor.lib.stack import app_stack
from harbor.lib.util import fmt_size


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "fetch",
    help="Download and install a happ from a GitHub repository",
  )
  parser.add_argument(
    "target",
    metavar="TARGET",
    help=USAGE.splitlines()[0],
  )
  parser.add_argument(
    "-y",
    "--yes",
    action="store_true",
    help="Install without asking for confirmation",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  target = parse_target(args.target)
  apps_root = ctx.config.apps_root

  # Fail on a name collision before spending any rate limit.
  destination_for(target.app_id, apps_root)

  staged = stage_happ(target, apps_root)
  committed = False
  try:
    stack = app_stack(staged.path)
    conn.out(capability_receipt(stack, None, ctx, compact=False))
    conn.out(
      f"\nfrom {target.describe(staged.sha)}"
      f" ({staged.files} files, {fmt_size(staged.total_bytes)})"
    )

    if not args.yes and not _confirmed(conn):
      conn.out("Not installed.")
      return

    dest = commit_happ(staged, apps_root)
    committed = True
  finally:
    # Covers every exit that is not a successful install: a declined prompt,
    # an invalid manifest, or a failure part-way through committing.
    if not committed:
      discard(staged)

  conn.out(f"Installed {staged.app_id} at {dest}")
  conn.out(f"Start it with: harbor start {staged.app_id}")


def _confirmed(conn) -> bool:
  """Ask before installing.

  Harbor cannot vouch for a happ's author, so the operator reading the receipt
  above is the check that matters. The images it names are pulled unverified.
  """
  try:
    answer = conn.read("\nInstall this happ? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")
