import argparse
from pathlib import Path

from harbor.lib.apps import read_last_app_action
from harbor.lib.happ import is_pathlike, load_happ
from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import RunState
from harbor.lib.receipt import capability_receipt
from harbor.lib.run_layout import load_run_data


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
    staged = ctx.staged_stack(app)
    stack = staged or ctx.bundle_stack(app)
    if stack is None:
      raise ValueError(
        f"No manifest for {app}: it is neither installed nor in a catalog. "
        f"Pass a path to a .happ to inspect one directly."
      )

    notes: tuple[str, ...] = ()
    if staged is None:
      notes = (
        f"{app} is not installed; this is the manifest it would be installed "
        f"from. Install it with `harbor install {app}`",
      )
    elif ctx.manifest_stale(app):
      notes = (
        f"manifest has changed, `harbor install {app}` may be required to "
        f"reflect changes",
      )

    conn.out(
      capability_receipt(
        stack,
        load_run_data(stack, ctx) if staged is not None else None,
        ctx,
        compact=False,
        notes=notes,
        state_line=_state_line(ctx.run_state(app)),
        last_action=read_last_app_action(app, ctx) or "-",
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
