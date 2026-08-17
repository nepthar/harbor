import argparse

from harbor.lib.fetch import (
  USAGE,
  commit_happ,
  discard,
  download_happ,
  ensure_destination_for,
  format_current,
  parse_current,
  parse_target,
  recorded_source,
  replace_happ,
  resolve_ref,
  source_is_pinned,
  split_pin,
)
from harbor.lib.happ import is_pathlike, load_happ
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import capability_receipt
from harbor.lib.util import fmt_size


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "fetch",
    help="Install a happ from GitHub, or update one already fetched",
  )
  parser.add_argument(
    "target",
    metavar="TARGET",
    help="github: URL to install, or APP[@sha] to update a fetched happ",
  )
  parser.add_argument(
    "-y",
    "--yes",
    action="store_true",
    help="Install without asking for confirmation",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
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
  target = parse_target(spec)
  if pin:
    target = target.at_sha(pin)
  apps_root = ctx.config.apps_root

  # Fail on a name collision before spending any rate limit. Other app
  # sources count: installing into apps/ over an id one of them already
  # carries would leave that id resolving to two places.
  ensure_destination_for(target.app_id, apps_root, target.suffix)
  elsewhere = ctx.app_catalog().get(str(target.app_id), ())
  if elsewhere:
    raise ValueError(
      f"{target.app_id} is already in the {elsewhere[0].source} app source at "
      f"{elsewhere[0].path}.\nRemove it first if you mean to replace it; "
      f"a github: fetch never overwrites an installed happ."
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

    if not yes and not _confirmed(conn):
      conn.out("Not installed.")
      return

    dest = commit_happ(fetched, apps_root)
    ctx.harbor_db().set_app_source(
      str(fetched.app_id),
      source=recorded_source(spec, pin),
      current=format_current(stack.version, fetched.sha),
    )
    committed = True
  finally:
    # Covers every exit that is not a successful install: a declined prompt,
    # an invalid manifest, or a failure part-way through committing.
    if not committed:
      discard(fetched)

  conn.out(f"Installed {fetched.app_id} at {dest}")
  conn.out(f"Start it with: harbor start {fetched.app_id}")


def _update(query: str, pin: str | None, ctx: HarborCtx, conn) -> None:
  try:
    app = ctx.resolve_app(query)
  except ValueError as e:
    raise ValueError(f"{e}. To fetch a new app, pass a github: target.") from e

  record = ctx.harbor_db().get_app_source(str(app))
  if not record:
    raise ValueError(
      f"{app} has no recorded GitHub source (it was not installed with "
      f"harbor fetch).\nRemove it first if you mean to replace it with a "
      f"fetched copy."
    )

  source = record["source"]
  current = record["current"]
  spec = split_pin(source)[0]

  if source_is_pinned(source) and not pin:
    conn.out(f"{app} is pinned at {current}")
    return

  target = parse_target(spec)
  if pin:
    target = target.at_sha(pin)

  resolved = resolve_ref(target)
  _, current_sha = parse_current(current)
  new_source = recorded_source(spec, pin)
  if resolved == current_sha:
    if new_source != source:
      ctx.harbor_db().set_app_source(str(app), source=new_source, current=current)
      conn.out(f"Pinned {app} at {current}")
      return
    conn.out(f"{app} is already at {current}")
    return

  dest = ctx.bundle_path(app)
  fetched = download_happ(target.at_sha(resolved), dest.parent)
  committed = False
  try:
    stack = load_happ(fetched.path).app_stack()
    replace_happ(fetched, dest)
    ctx.harbor_db().set_app_source(
      str(app),
      source=new_source,
      current=format_current(stack.version, fetched.sha),
    )
    committed = True
  finally:
    if not committed:
      discard(fetched)

  new_current = format_current(stack.version, fetched.sha)
  conn.out(f"Updated {app}")
  conn.out(f" - {current}")
  conn.out(f" + {new_current}")
  if ctx.is_staged(app):
    conn.out(
      "Take a snapshot before staging and starting the new version:\n"
      f"  harbor snapshot {app}\n"
      f"  harbor stage {app}\n"
      f"  harbor start {app}"
    )


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
