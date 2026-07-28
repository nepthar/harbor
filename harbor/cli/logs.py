import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import logs


def register(subparsers) -> None:
  parser = subparsers.add_parser("logs", help="Show logs for an installed happ")
  parser.add_argument(
    "-f",
    "--follow",
    action="store_true",
    help="Follow log output",
  )
  parser.add_argument(
    "--tail",
    metavar="N",
    help="Number of lines to show from the end of the logs",
  )
  parser.add_argument("app_id", help="App ID of the happ")
  parser.add_argument(
    "passthrough",
    nargs=argparse.REMAINDER,
    help="Extra args after -- passed to docker compose logs",
  )
  # `logs -f` streams until the operator interrupts it. Holding the harbor lock
  # for that long would shut every other harbor command out of the machine for
  # as long as someone is watching logs, so this command runs without it. It
  # only reads container output; it changes no harbor state.
  parser.set_defaults(func=run, holds_lock=False)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  extra: list[str] = []
  if args.follow:
    extra.append("--follow")
  if args.tail is not None:
    extra.extend(["--tail", str(args.tail)])
  passthrough = list(args.passthrough or [])
  if passthrough and passthrough[0] == "--":
    passthrough = passthrough[1:]
  extra.extend(passthrough)
  state = ctx.run_state(args.app_id)
  try:
    logs(state.app_id, extra, ctx)
  except KeyboardInterrupt:
    raise SystemExit(130) from None
