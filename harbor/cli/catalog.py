import argparse
from pathlib import Path

from tabulate import tabulate

from harbor.lib.apps import read_app_actions
from harbor.lib.harbor import CatalogEntry, HarborCtx


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "catalog", help="List available happ bundles and the app source each is in"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  catalog = ctx.catalog()
  staged = ctx.staged_app_ids()
  # One read of the activity log for every app, rather than one per row.
  actions = read_app_actions(ctx.config)
  origins = {app_id: ctx.staged_origin(app_id) for app_id in staged}

  # An id carried by two app sources gets a row per source, which is what
  # makes the ambiguity `doctor` reports visible here too.
  rows = [
    (app_id, entry.source, _status(entry, staged, origins, actions), str(entry.path))
    for app_id in sorted(catalog)
    for entry in catalog[app_id]
  ]
  conn.out(
    tabulate(rows, headers=["APP_ID", "SOURCE", "STATUS", "PATH"], tablefmt="simple")
  )


def _status(
  entry: CatalogEntry,
  staged: set[str],
  origins: dict[str, Path | None],
  actions: dict[str, str],
) -> str:
  """The last thing harbor did with this bundle, or how it stands if nothing yet.

  Only for the entry actually installed: an app id is staged from exactly one
  catalog entry, so when two carry the id the other is not what is running,
  and saying otherwise would credit it with the wrong state. The comparison is
  by path as written, not resolved: two entries that symlink to one directory
  are still two entries, and only the one `stage` recorded is the installed
  one. Only for installed apps at all, too -- the activity log outlives
  `harbor rm`, and a leftover action would read as if the app were still there.
  """
  if entry.app_id not in staged:
    return "-"

  origin = origins.get(entry.app_id)
  if origin is not None and origin != entry.path:
    return "-"

  return actions.get(entry.app_id, "installed")
