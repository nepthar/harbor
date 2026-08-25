import argparse
import gc
import logging
import sys

from harbor import VERSION
from harbor.cli import (
  activity,
  catalog,
  cmd,
  config,
  config_sys,
  decrypt,
  dev,
  doctor,
  fetch,
  init,
  inspect,
  logs,
  ps,
  restore,
  rm,
  routes,
  snapshot,
  stage,
  start,
  stop,
  volumes,
)
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.util import Conn, refuse_root

for level, name in [
  (logging.DEBUG, "debug"),
  (logging.INFO, "info "),
  (logging.WARNING, "warn "),
  (logging.ERROR, "error"),
  (logging.CRITICAL, "crit "),
]:
  logging.addLevelName(level, name)

COMMANDS = [
  catalog,
  init,
  doctor,
  ps,
  inspect,
  volumes,
  stage,
  start,
  dev,
  stop,
  rm,
  snapshot,
  restore,
  logs,
  activity,
  cmd,
  fetch,
  config,
  config_sys,
  decrypt,
  routes,
]


class StdConn(Conn):
  def out(self, data):
    print(data)

  def err(self, data):
    print(data, file=sys.stderr)

  def read(self, prompt: str = "") -> str:
    return input(prompt)


def _lock_description(args: argparse.Namespace) -> str:
  """What to record as the lock holder: the command and its app, nothing more.

  Deliberately not `sys.argv`, which would put `config --set pass=hunter2` into
  a plaintext log that outlives the command.
  """
  for attr in ("app", "app_id", "target"):
    target = getattr(args, attr, None)
    if isinstance(target, str) and target:
      return f"{args.command} {target}"
  return str(args.command)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="harbor", description="Harbor CLI")
  parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
  parser.add_argument(
    "--root",
    metavar="DIR",
    help="Harbor root directory (overrides HARBOR_ROOT)",
  )
  parser.add_argument(
    "--config",
    metavar="FILE",
    help="Path to config.toml (overrides HARBOR_CONFIG / --root)",
  )
  parser.set_defaults(func=lambda args, ctx, conn: parser.print_help())
  subparsers = parser.add_subparsers(dest="command")

  for command in COMMANDS:
    command.register(subparsers)

  return parser


def _dispatch(args: argparse.Namespace, conn: Conn) -> None:
  try:
    # Before anything else, and before `init` in particular: the first command
    # is the one that would create the harbor root with the wrong owner.
    refuse_root("harbor")
    if args.command is None or args.command == "init":
      args.func(args, None, conn)
    else:
      cfg = load_config(
        config_path=getattr(args, "config", None),
        root=getattr(args, "root", None),
      )
      if not cfg:
        raise ValueError("Harbor is not initialized; run `harbor init` first")
      ctx = HarborCtx(cfg)
      # Commands opt out with `set_defaults(holds_lock=False)` when they are
      # long-running and change no state -- see harbor/cli/logs.py.
      if getattr(args, "holds_lock", True):
        with ctx.lock(_lock_description(args)):
          args.func(args, ctx, conn)
      else:
        args.func(args, ctx, conn)
  except KeyboardInterrupt:
    raise SystemExit(130) from None
  except (RuntimeError, ValueError) as error:
    conn.err(f"Error: {error}")
    raise SystemExit(1) from error


def run(argv: list[str] | None = None, conn: Conn | None = None) -> int:
  """Execute one harbor command and return its exit code.

  Separate from `main` so the test suite can call it in-process. Every exit
  path in the CLI is a `SystemExit` (argparse's own, plus the commands that
  raise it directly), so catching it here is what turns a command into a
  code rather than a dead interpreter.

  Logging is (re)configured per call against the *current* `sys.stderr`, so a
  caller that redirects the stream sees warnings as well as `conn` output.
  """
  logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
    force=True,
  )
  parser = build_parser()
  try:
    _dispatch(parser.parse_args(argv), conn or StdConn())
  except SystemExit as exit_:
    if exit_.code is None:
      return 0
    return exit_.code if isinstance(exit_.code, int) else 1
  return 0


def main() -> None:
  # Harbor commands are short-lived and allocate little that cycles; skipping
  # collection is worth ~10ms of a 120ms invocation. Deliberately not at import
  # time -- `run` is called in-process by the tests, which do need a collector.
  gc.disable()
  raise SystemExit(run())
