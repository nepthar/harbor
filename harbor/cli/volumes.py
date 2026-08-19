import argparse

from tabulate import tabulate

from harbor.lib.harbor import HarborCtx
from harbor.lib.util import fmt_size, path_size


def register(subparsers) -> None:
  parser = subparsers.add_parser("volumes", help="List all volumes with their sizes")
  parser.set_defaults(func=run)


def run(_args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  rows = []

  for volume_type, root in ctx.config.volume_roots.items():
    if not root.is_dir():
      continue
    for app_dir in root.iterdir():
      if not app_dir.is_dir():
        continue
      for volume_dir in app_dir.iterdir():
        if not volume_dir.is_dir():
          continue
        rows.append(
          (
            app_dir.name,
            volume_dir.name,
            volume_type,
            fmt_size(path_size(volume_dir)),
          )
        )

  rows.sort()
  conn.out(
    tabulate(rows, headers=["app_id", "volume", "type", "size"], tablefmt="simple")
  )
