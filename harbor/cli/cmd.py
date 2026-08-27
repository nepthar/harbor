import argparse

from tabulate import tabulate

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import run_command
from harbor.lib.stack import AppStack


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "cmd",
    help="List or run commands defined in a happ's manifest",
  )
  parser.add_argument("app_id", help="App ID of the happ")
  parser.add_argument(
    "cmd_name",
    nargs="?",
    default=None,
    help="Command name from [commands]; omit to list",
  )
  parser.add_argument(
    "args",
    nargs=argparse.REMAINDER,
    help="Arguments forwarded to the command",
  )
  # Listing is a short read. Running holds the app lock so stage/start/stop
  # of this app wait; the harbor lock is left free so other apps can proceed.
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  app = ctx.resolve_app(args.app_id)
  if args.cmd_name is None:
    _list_commands(app, ctx, conn)
    return

  extra = list(args.args or [])
  if extra and extra[0] == "--":
    extra = extra[1:]
  with ctx.app_lock(app, f"cmd {app}"):
    code = run_command(app, args.cmd_name, extra, ctx)
  raise SystemExit(code)


def _list_commands(app, ctx: HarborCtx, conn) -> None:
  paths = ctx.staged_paths(app)
  if not paths.compose_path.is_file():
    raise ValueError(f"App {app} is not installed; run `harbor install {app}` first")

  stack = AppStack.from_file(paths.manifest_path, app)
  if not stack.commands:
    conn.out(f"No commands defined for {app}")
    return

  rows = [
    (name, entry.desc or "-", entry.run_unit)
    for name, entry in sorted(stack.commands.items())
  ]
  conn.out(
    tabulate(rows, headers=["COMMAND", "DESCRIPTION", "RUN_UNIT"], tablefmt="simple")
  )
