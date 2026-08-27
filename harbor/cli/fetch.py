import argparse

from harbor.lib.fetch import (
  USAGE,
  FetchResult,
  install_target,
  parse_target,
  preview_target,
  refuse_existing,
  split_pin,
  update_app,
)
from harbor.lib.happ import is_pathlike
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import capability_receipt
from harbor.lib.util import fmt_size


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "fetch",
    help="Fetch a happ from GitHub, or update one already fetched",
  )
  parser.add_argument(
    "target",
    metavar="TARGET",
    help="github: URL to fetch, or APP[@sha] to update a fetched happ",
  )
  parser.add_argument(
    "-y",
    "--yes",
    action="store_true",
    help="Fetch without asking for confirmation",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  with ctx.harbor_lock(f"fetch {args.target}"):
    spec, pin = split_pin(args.target)
    if spec.startswith("github:"):
      _install(spec, pin, args.yes, ctx, conn)
    elif is_pathlike(spec):
      raise ValueError(
        f"Don't know how to fetch {args.target!r}; expected a github: target "
        f"or an installed app id.\n  {USAGE}"
      )
    elif "@" in args.target and pin is None:
      raise ValueError(
        f"Pin must be a full 40-character commit sha, not "
        f"{args.target.rsplit('@', 1)[-1]!r}"
      )
    else:
      _update(spec, pin, ctx, conn)


def _install(spec: str, pin: str | None, yes: bool, ctx: HarborCtx, conn) -> None:
  """Show what is at the target, ask, then fetch it for real.

  The preview downloads and throws away, so what the operator approves is read
  from the actual files rather than from the URL they typed. `install_target`
  then re-downloads at the same pinned sha.
  """
  refuse_existing(parse_target(spec), ctx)
  preview = preview_target(spec, pin, ctx)

  conn.out(capability_receipt(preview.stack, None, ctx, compact=False))
  conn.out(
    f"\nfrom {parse_target(spec).describe(preview.sha)}"
    f" ({preview.files} files, {fmt_size(preview.total_bytes)})"
  )

  if not yes and not _confirmed(conn):
    conn.out("Not fetched.")
    return

  result = install_target(spec, pin, ctx, at_sha=preview.sha)
  conn.out(f"App {result.app_id} is now available for install, at {result.path}")
  conn.out(f"Install it with: harbor install {result.app_id}")


def _update(query: str, pin: str | None, ctx: HarborCtx, conn) -> None:
  try:
    app = ctx.resolve_app(query)
  except ValueError as e:
    raise ValueError(f"{e}. To fetch a new app, pass a github: target.") from e

  result = update_app(app, pin, ctx)
  if not isinstance(result, FetchResult):
    conn.out(result)
    return

  conn.out(f"Updated {app}")
  conn.out(f" - {result.previous}")
  conn.out(f" + {result.current}")
  if result.staged:
    conn.out(
      "Take a snapshot before staging and starting the new version:\n"
      f"  harbor snapshot {app}\n"
      f"  harbor install {app}\n"
      f"  harbor start {app}"
    )


def _confirmed(conn) -> bool:
  """Ask before fetching.

  Harbor cannot vouch for a happ's author, so the operator reading the receipt
  above is the check that matters. The images it names are pulled unverified.
  """
  try:
    answer = conn.read("\nFetch this happ? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")
