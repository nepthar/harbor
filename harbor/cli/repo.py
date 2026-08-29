"""`harbor repo` -- the sources the catalog is built from."""

import argparse
import shutil

from harbor.lib.config import load_config_file
from harbor.lib.config_edit import add_repo, remove_repo
from harbor.lib.harbor import HarborCtx
from harbor.lib.repo import MAIN_REPO, USAGE, mirror, name_from_url
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
  location = args.location
  is_url = "://" in location
  name = args.name or (name_from_url(location) if is_url else "")
  if not name:
    raise ValueError("A local repo needs a name: harbor repo add <dir> --name <name>")

  before = set(ctx.app_catalog())
  with ctx.locked("repo add"):
    if is_url:
      add_repo(ctx, name, url=location)
    else:
      add_repo(ctx, name, path=location)
    conn.out(f"Added repo {name} -> {location}")

    if is_url:
      # The repo it just wrote is not in the Config this process loaded, so
      # re-read the file rather than reconstructing the entry by hand.
      fresh = HarborCtx(load_config_file(ctx.config.config_path))
      result = mirror(fresh.config.repos[name], fresh)
      conn.out(f"Mirrored {len(result.happs)} happs at {result.sha[:8]}")
      _report_collisions(fresh, before, conn)


def _update(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  wanted = [_one(ctx, args.name)] if args.name else list(ctx.config.repos.values())
  mirrored = [repo for repo in wanted if repo.mirrored]
  if not mirrored:
    conn.out("No mirrored repos to update.")
    return

  with ctx.locked("repo update"):
    for repo in mirrored:
      before = set(ctx.app_catalog())
      result = mirror(repo, ctx)
      if result.unchanged:
        conn.out(f"{repo.name}: already at {result.sha[:8]}")
        continue
      conn.out(
        f"{repo.name}: {result.sha[:8]} "
        f"({len(result.happs)} happs, {fmt_size(result.total_bytes)})"
      )
      _report_collisions(ctx, before, conn)


def _remove(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  repo = _one(ctx, args.name)
  if repo.name == MAIN_REPO:
    raise ValueError(f"{MAIN_REPO} is built in and cannot be removed")

  bound = sorted(_bound_to_repo(ctx, repo.name))
  if bound:
    conn.err(
      f"These apps were installed from {repo.name}: {', '.join(bound)}.\n"
      f"They keep running -- what is staged under run/ is already a copy -- but "
      f"harbor will no longer see updates for them."
    )

  with ctx.locked("repo remove"):
    remove_repo(ctx, repo.name)
    if repo.mirrored:
      shutil.rmtree(repo.path, ignore_errors=True)
      ctx.harbor_db.del_repo_state(repo.name)
    conn.out(f"Removed repo {repo.name}")


def _list(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  catalog = ctx.app_catalog()
  for name, repo in ctx.config.repos.items():
    count = sum(1 for entries in catalog.values() for e in entries if e.source == name)
    state = ctx.harbor_db.get_repo_state(name) if repo.mirrored else None
    at = f"  {state['sha'][:8]}" if state else ""
    conn.out(f"{name:16} {count:>3} apps  {repo.describe()}{at}")


def _one(ctx: HarborCtx, name: str):
  repo = ctx.config.repos.get(name)
  if repo is None:
    known = ", ".join(sorted(ctx.config.repos))
    raise ValueError(f"No repo {name!r}; configured repos: {known}.")
  return repo


def _bound_to_repo(ctx: HarborCtx, name: str) -> set[str]:
  from harbor.lib.lifecycle import bound_to

  return {
    app_id
    for app_id in ctx.config.app_config_ids()
    if bound_to(app_id, ctx) == f"repo {name}"
  }


def _report_collisions(ctx: HarborCtx, before: set[str], conn: Conn) -> None:
  """Name every id that is now carried by more than one repo.

  A collision is not an error -- two repos may legitimately both ship mealie --
  but it changes how the app is installed, so it is never left silent.
  """
  for app_id, repos in sorted(ctx.contested_app_ids().items()):
    if app_id in before and len(repos) < 2:
      continue
    conn.err(
      f"{app_id} is now in {len(repos)} repos ({', '.join(sorted(repos))}); "
      f"install it as {app_id}@<repo>."
    )
