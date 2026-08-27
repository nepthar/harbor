import argparse
from datetime import datetime
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
  with ctx.harbor_lock("catalog"):
    catalog = ctx.app_catalog()
    staged = ctx.staged_app_ids()
    # One read of the activity log for every app, rather than one per row.
    actions = read_app_actions(ctx)
    origins = {app_id: ctx.staged_origin(app_id) for app_id in staged}

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
  actions: dict[str, tuple[datetime, str]],
) -> str:
  """The last thing harbor did with this bundle, or how it stands if nothing yet."""
  if entry.app_id not in staged:
    return "-"

  origin = origins.get(entry.app_id)
  if origin is not None and origin != entry.path:
    return "-"

  action = actions.get(entry.app_id)
  return action[1] if action else "installed"
