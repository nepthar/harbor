import argparse

from harbor.lib.fetch import (
  USAGE,
  commit_happ,
  discard,
  download_happ,
  ensure_destination_for,
  parse_target,
)
from harbor.lib.happ import load_happ
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import capability_receipt
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

  # Fail on a name collision before spending any rate limit. Other app
  # sources count: installing into apps/ over an id one of them already
  # carries would leave that id resolving to two places.
  ensure_destination_for(target.app_id, apps_root, target.suffix)
  elsewhere = ctx.catalog().get(str(target.app_id), ())
  if elsewhere:
    raise ValueError(
      f"{target.app_id} is already in the {elsewhere[0].source} app source at "
      f"{elsewhere[0].path}.\nRemove it first if you mean to replace it; "
      f"harbor fetch never overwrites an installed happ."
    )

  fetched = download_happ(target, apps_root)
  committed = False
  try:
    # `load_happ` handles both flavors, and for a .happ.md this parse is also
    # the content validation the folder flow gets from its tree listing.
    stack = load_happ(fetched.path).app_stack()
    conn.out(capability_receipt(stack, None, ctx, compact=False))
    conn.out(
      f"\nfrom {target.describe(fetched.sha)}"
      f" ({fetched.files} files, {fmt_size(fetched.total_bytes)})"
    )

    if not args.yes and not _confirmed(conn):
      conn.out("Not installed.")
      return

    dest = commit_happ(fetched, apps_root)
    committed = True
  finally:
    # Covers every exit that is not a successful install: a declined prompt,
    # an invalid manifest, or a failure part-way through committing.
    if not committed:
      discard(fetched)

  conn.out(f"Installed {fetched.app_id} at {dest}")
  conn.out(f"Start it with: harbor start {fetched.app_id}")


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
