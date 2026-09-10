import argparse
from pathlib import Path

from kelso.lib.bundle import load_bundle
from kelso.lib.kelso import KelsoCtx
from kelso.lib.lifecycle import stage, staging_target
from kelso.lib.stack import ComposeWarning
from kelso.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "install",
    help="Install an application so it can be started (accepts app id or .klso path)",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID (e.g. io.example.myapp or myapp) or path to an app",
  )
  parser.add_argument(
    "--force",
    action="store_true",
    help="Install even though this id was last installed from somewhere else",
  )
  parser.add_argument(
    "-y",
    "--yes",
    action="store_true",
    help="Skip the confirmation for compose keys kelso does not model",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: KelsoCtx, conn: Conn) -> None:
  target = staging_target(ctx, args.app, force=args.force)
  app = target.app_id
  bundle = target.bundle or ctx.bundle_path(app)
  if not args.yes and not _confirmed(app, bundle, conn):
    conn.out("Nothing installed.")
    return
  with ctx.locked(f"stage {app}", app):
    result = stage(app, bundle, ctx, bound=target.bound_to)
    for name in result.dropped_volumes:
      conn.err(
        f"volume {name} is no longer declared in the manifest; "
        f"its link is gone but its data was left in place"
      )
    conn.out(f"Installed {app} at {ctx.run_path(app)}")
    conn.out(f"Start it with: kelso start {app}")


def _compose_warnings(bundle: Path) -> tuple[ComposeWarning, ...]:
  """This bundle's off-allowlist compose keys, or none if it does not parse.

  A manifest that cannot be read has nothing to warn about yet -- `stage` is
  about to fail on it with a better message than a prompt could give.
  """
  try:
    return load_bundle(bundle).app_stack().compose_warnings
  except (ValueError, RuntimeError, OSError):
    return ()


def _confirmed(app: str, bundle: Path, conn: Conn) -> bool:
  """Ask only when the manifest passes something through unmodelled."""
  warnings = _compose_warnings(bundle)
  if not warnings:
    return True

  for warning in warnings:
    conn.out(f"Warning: {warning.message()}:")
    for line in warning.option_lines():
      conn.out(f"  {line}")

  try:
    answer = conn.read(f"Install {app} anyway? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")
