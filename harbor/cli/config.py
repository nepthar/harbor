import argparse

from tabulate import tabulate

from harbor.cli.kv import parse_kv
from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import apply_config_sets, bind
from harbor.lib.stack import AppStack, app_stack
from harbor.lib.store import AppStore


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "config",
    help="List or set happ config and external volume binds",
  )
  parser.add_argument(
    "app",
    metavar="APP",
    help="App ID of a staged happ",
  )
  parser.add_argument(
    "--set",
    action="append",
    default=[],
    dest="sets",
    metavar="KEY=VALUE",
    help="Set a config value (repeatable)",
  )
  parser.add_argument(
    "--bind",
    action="append",
    default=[],
    dest="binds",
    metavar="VOLUME=HOST_PATH",
    help="Bind an external volume to a host path (repeatable)",
  )
  parser.add_argument(
    "--get",
    dest="get_name",
    metavar="NAME",
    help="Print a single config value (secrets show as 'set' unless --show-secret)",
  )
  parser.add_argument(
    "--show-secret",
    action="store_true",
    help="With --get, print secret values in plaintext",
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  app = ctx.resolve_app(args.app)
  # Schema from the staged run copy; values from the run-dir config store.
  # `harbor start --set` is the one-shot for first install.
  stack = app_stack(ctx.app_path(app), app)
  store = ctx.app_store(app)

  if args.get_name is not None:
    if args.sets or args.binds:
      raise ValueError("--get cannot be combined with --set or --bind")
    _get(stack, store, args.get_name, conn, show_secret=args.show_secret)
    return

  if args.show_secret:
    raise ValueError("--show-secret requires --get")

  if args.sets or args.binds:
    _apply(app, stack, args.sets, args.binds, ctx, conn)
    return

  _list(app, stack, store, conn)


def _get(
  stack: AppStack,
  store: AppStore,
  name: str,
  conn,
  *,
  show_secret: bool,
) -> None:
  config = stack.config.get(name)
  if not config:
    raise ValueError(f"config {name!r} not declared in manifest")

  secret, value = store.get_config(name)
  if value is None:
    if config.has_default():
      conn.err(f"Config {config.name} using default value")
      conn.out(config.default)
    else:
      raise SystemExit(1)
  elif secret and not show_secret:
    conn.out("set")
  else:
    conn.out(value)


def _apply(
  app: AppID,
  stack: AppStack,
  sets_raw: list[str],
  binds_raw: list[str],
  ctx: HarborCtx,
  conn,
) -> None:
  sets = [parse_kv(item, "--set") for item in sets_raw]
  binds = [parse_kv(item, "--bind") for item in binds_raw]

  if sets:
    apply_config_sets(stack, sets, ctx)
  for volname, host_path in binds:
    bind(stack, volname, host_path, ctx)

  try:
    state = ctx.run_state(app)
  except ValueError:
    state = None
  if state is not None and state.running_count:
    conn.err(
      f"App {app} is running; run `harbor stop {app}` "
      f"&& `harbor start {app}` to apply new config"
    )


def _list(app: AppID, stack: AppStack, store: AppStore, conn) -> None:
  rows = []
  for name, entry in stack.config.items():
    secret, value = store.get_config(name)
    if value is None:
      if entry.has_default():
        display = f"{entry.default} (default)"
      else:
        display = "(required)"
    elif secret:
      display = "(secret)"
    else:
      display = value
    rows.append([name, display, entry.desc or ""])
  conn.out(f"Configuration parameters for: {app}")
  conn.out(tabulate(rows, headers=["name", "value", "description"]))

  ext = [(n, v) for n, v in stack.volumes.items() if v.kind == "ext"]
  if not ext:
    return

  binds = store.list_binds()
  bind_rows = []
  for name, _volume in ext:
    entry = binds.get(name)
    host_path = entry["host_path"] if entry else "(not bound)"
    bind_rows.append([name, host_path])
  conn.out("")
  conn.out("External volume binds:")
  conn.out(tabulate(bind_rows, headers=["volume_name", "host_path"], tablefmt="simple"))
