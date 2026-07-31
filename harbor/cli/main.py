import argparse
import gc
import logging
import sys

from harbor.cli import (
  catalog,
  config,
  config_sys,
  doctor,
  fetch,
  init,
  inspect,
  logs,
  ps,
  rm,
  routes,
  snapshot,
  stage,
  start,
  status,
  stop,
  volumes,
)
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.util import Conn

VERSION = "0.1.0"

gc.disable()


for level, name in [
  (logging.DEBUG, "debug"),
  (logging.INFO, "info "),
  (logging.WARNING, "warn "),
  (logging.ERROR, "error"),
  (logging.CRITICAL, "crit "),
]:
  logging.addLevelName(level, name)

logging.basicConfig(
  level=logging.WARNING,
  format="%(levelname)s %(name)s: %(message)s",
)

COMMANDS = [
  catalog,
  init,
  doctor,
  ps,
  status,
  volumes,
  stage,
  start,
  stop,
  rm,
  snapshot,
  logs,
  fetch,
  inspect,
  config,
  config_sys,
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


def main() -> None:
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

  args = parser.parse_args()
  conn = StdConn()

  if args.command is None:
    args.func(args, None, conn)
    return

  try:
    if args.command == "init":
      args.func(args, None, conn)
    else:
      cfg = load_config(
        "cli",
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
