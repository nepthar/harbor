"""`harbor repo` -- the sources the catalog is built from."""

import argparse

from harbor.lib import repo as repo_lib
from harbor.lib.config import load_config_file
from harbor.lib.harbor import HarborCtx
from harbor.lib.repo import USAGE
from harbor.lib.util import Conn, fmt_size


def register(subparsers) -> None:
  parser = subparsers.add_parser("repo", help="Manage the repos apps come from")
  sub = parser.add_subparsers(dest="repo_command", required=True)

  add = sub.add_parser("add", help="Add a repo of happs", description=USAGE)
  add.add_argument("location", help="A github:// url, or a directory on this machine")
  add.add_argument(
    "--name", default="", help="Name it something other than the default"
  )
  add.set_defaults(func=_add)

  update = sub.add_parser("update", help="Bring mirrored repos up to the remote")
  update.add_argument("name", nargs="?", default="", help="One repo, or all of them")
  update.set_defaults(func=_update)

  remove = sub.add_parser("remove", help="Drop a repo and its mirrored copy")
  remove.add_argument("name", help="Repo to remove")
  remove.set_defaults(func=_remove)

  listing = sub.add_parser("list", help="Show configured repos")
  listing.set_defaults(func=_list)


def _add(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  result = repo_lib.add(ctx, args.location, name=args.name)
  conn.out(f"Added repo {result.repo.name} -> {result.repo.describe()}")
  if result.mirrored is not None:
    done = result.mirrored
    conn.out(
      f"Mirrored {len(done.happs)} happs at {done.sha[:8]} "
      f"({fmt_size(done.total_bytes)})"
    )
  _report_contested(ctx, conn)


def _update(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  results = repo_lib.update(ctx, args.name)
  if not results:
    conn.out("No mirrored repos to update.")
    return
  for result in results:
    if result.unchanged:
      conn.out(f"{result.name}: already at {result.sha[:8]}")
    else:
      conn.out(
        f"{result.name}: {result.sha[:8]} "
        f"({len(result.happs)} happs, {fmt_size(result.total_bytes)})"
      )
  _report_contested(ctx, conn)


def _remove(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  result = repo_lib.remove(ctx, args.name)
  if result.bound:
    conn.err(
      f"These apps were installed from {result.name}: {', '.join(result.bound)}.\n"
      f"They keep running -- what is staged under run/ is already a copy -- but "
      f"harbor will no longer see updates for them."
    )
  conn.out(f"Removed repo {result.name}")


def _list(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  catalog = ctx.app_catalog()
  for name, repo in ctx.config.repos.items():
    count = sum(1 for entries in catalog.values() for e in entries if e.source == name)
    state = ctx.harbor_db.get_repo_state(name) if repo.mirrored else None
    at = f"  {state['sha'][:8]}" if state else ""
    conn.out(f"{name:16} {count:>3} apps  {repo.describe()}{at}")


def _report_contested(ctx: HarborCtx, conn: Conn) -> None:
  # `ctx.config` predates the change.
  fresh = HarborCtx(load_config_file(ctx.config.config_path))
  for line in repo_lib.contested_lines(fresh):
    conn.err(line)
