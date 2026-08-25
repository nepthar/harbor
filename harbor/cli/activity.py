import argparse

from harbor.lib import activity
from harbor.lib.harbor import HarborCtx


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "activity",
    help="What harbor ran unattended, and what each run printed",
  )
  parser.add_argument(
    "app_id",
    nargs="?",
    help='Narrow to one app (or "harbor" for runs that name no app)',
  )
  parser.add_argument(
    "-n",
    "--last",
    type=int,
    default=20,
    metavar="N",
    help="How many runs to list (default: 20)",
  )
  parser.add_argument(
    "--show",
    nargs="?",
    const=1,
    type=int,
    metavar="N",
    help="Print the output of the Nth listed run (default: 1, the newest)",
  )
  # Reads the index and, with --show, one file; changes nothing.
  parser.set_defaults(func=run, holds_lock=False)


def _resolve(query: str | None, ctx: HarborCtx) -> str | None:
  """Full id for a stem, or the query as given: a removed app's runs are
  still on record, and refusing to list them would hide exactly the history
  an operator goes looking for after an `rm`."""
  if not query or query == activity.HARBOR_DIR:
    return query
  try:
    return str(ctx.resolve_app(query))
  except (ValueError, RuntimeError):
    return query


def _duration(ms: int | None) -> str:
  if ms is None:
    return "-"
  return f"{ms / 1000:.1f}s"


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  app = _resolve(args.app_id, ctx)
  runs = activity.list_runs(ctx.config, app=app, limit=max(args.last, args.show or 0))

  if not runs:
    where = f" for {app}" if app else ""
    conn.out(f"No recorded activity{where}. Runs land here as harbord executes jobs.")
    return

  if args.show is not None:
    if not 1 <= args.show <= len(runs):
      raise ValueError(f"--show {args.show}: only {len(runs)} run(s) on record")
    entry = runs[args.show - 1]
    if not entry["available"]:
      raise ValueError(
        f"The output of that run ({entry['log']}) has been pruned; "
        f"its index record above is all that remains"
      )
    dirname, _, filename = entry["log"].partition("/")
    conn.out(activity.read_run_log(ctx.config, dirname, filename).rstrip("\n"))
    return

  for index, entry in enumerate(runs, start=1):
    what = f"{entry['verb']} {entry['app_id'] or ''}".strip()
    log = entry["log"] if entry["available"] else f"{entry['log']} (pruned)"
    conn.out(
      f"{index:>3}  {entry['ts']}  {entry['status']:<5}  "
      f"{_duration(entry['duration_ms']):>7}  {what:<24}  logs/{log}"
    )
