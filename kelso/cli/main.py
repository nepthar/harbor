import argparse
import gc
import logging
import sys

from kelso import VERSION
from kelso.cli import (
  activity,
  catalog,
  cmd,
  config,
  config_sys,
  decrypt,
  dev,
  doctor,
  init,
  inspect,
  install,
  logs,
  ps,
  reload,
  remove,
  repo,
  restore,
  routes,
  snapshot,
  start,
  stop,
  volumes,
)
from kelso.lib.config import load_config
from kelso.lib.kelso import KelsoCtx
from kelso.lib.util import Conn, refuse_root

COMMANDS = [
  catalog,
  init,
  doctor,
  ps,
  inspect,
  volumes,
  install,
  start,
  dev,
  stop,
  reload,
  remove,
  snapshot,
  restore,
  logs,
  activity,
  cmd,
  repo,
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


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="kelso", description="Kelso CLI")
  parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
  parser.add_argument(
    "--root",
    metavar="DIR",
    help="Kelso root directory (overrides KELSO_ROOT)",
  )
  parser.add_argument(
    "--config",
    metavar="FILE",
    help="Path to config.toml (overrides KELSO_CONFIG / --root)",
  )
  parser.set_defaults(func=lambda args, ctx, conn: parser.print_help())
  subparsers = parser.add_subparsers(dest="command")

  for command in COMMANDS:
    command.register(subparsers)

  return parser


def _dispatch(args: argparse.Namespace, conn: Conn) -> None:
  try:
    # Before anything else, and before `init` in particular: the first command
    # is the one that would create the kelso root with the wrong owner.
    refuse_root("kelso")
    if args.command is None or args.command == "init":
      args.func(args, None, conn)
    else:
      cfg = load_config(
        config_path=getattr(args, "config", None),
        root=getattr(args, "root", None),
      )
      if not cfg:
        raise ValueError("Kelso is not initialized; run `kelso init` first")
      args.func(args, KelsoCtx(cfg), conn)
  except KeyboardInterrupt:
    raise SystemExit(130) from None
  except (RuntimeError, ValueError) as error:
    conn.err(f"Error: {error}")
    raise SystemExit(1) from error


def run(argv: list[str] | None = None, conn: Conn | None = None) -> int:
  """Execute one kelso command and return its exit code.

  Logging is reconfigured per call against the current `sys.stderr`, so a
  caller that redirects the stream still sees warnings.
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
  # Not at import time: `run` is called in-process by the tests, which do
  # need a collector.
  gc.disable()
  raise SystemExit(run())
