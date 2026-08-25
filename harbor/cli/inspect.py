import argparse
from pathlib import Path

from harbor.lib.apps import read_last_app_action
from harbor.lib.happ import is_pathlike, load_happ
from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import RunState
from harbor.lib.receipt import capability_receipt
from harbor.lib.run_layout import load_run_data
from harbor.lib.stack import AppStack


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "inspect",
    help="Show state, images, ports, routes, volumes, config, and sharp edges",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID or path to a harbor app",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  if is_pathlike(args.app):
    source = Path(args.app).expanduser().resolve()
    with ctx.harbor_lock(f"inspect {source}"):
      stack = load_happ(source).app_stack()
      conn.out(capability_receipt(stack, None, ctx, compact=False))
    return

  app = ctx.resolve_app(args.app)
  with ctx.locked(f"inspect {app}", app):
    # Report what is installed under run/, never the catalog entry under apps/.
    # Pass a path to a .happ to inspect a bundle that is not staged yet.
    stack = AppStack.from_file(ctx.staged_paths(app).manifest_path, app)
    run_data = load_run_data(stack, ctx)
    notes = ()
    if ctx.manifest_stale(app):
      notes = (
        f"manifest has changed, `harbor stage {app}` may be required to reflect changes",
      )
    conn.out(
      capability_receipt(
        stack,
        run_data,
        ctx,
        compact=False,
        notes=notes,
        state_line=_state_line(ctx.run_state(app)),
        last_action=read_last_app_action(app, ctx.config) or "-",
        show_logs=True,
      )
    )


def _state_line(state: RunState) -> str:
  total = len(state.containers)
  if state.running_count:
    return f"running, {state.running_count}/{total or state.running_count} containers"
  if total:
    return f"exited, 0/{total} containers"
  return "-"
