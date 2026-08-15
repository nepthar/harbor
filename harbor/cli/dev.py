import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import DevPlan, dev, dev_plan
from harbor.lib.receipt import host_port_lines
from harbor.lib.util import Conn


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "dev",
    help="Run a staged app in this terminal with its happ mounted from source",
  )
  parser.add_argument("app", metavar="APP", help="App ID of a staged app")
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn: Conn) -> None:
  app = ctx.resolve_app(args.app)
  plan = dev_plan(app, ctx)

  if plan.manifest_stale and not _confirmed(plan, conn):
    conn.out("Nothing started.")
    return

  conn.out(_receipt(plan))

  code = dev(plan, ctx)
  if code:
    raise SystemExit(code)


def _confirmed(plan: DevPlan, conn: Conn) -> bool:
  """A dev run against a manifest the operator has already moved past.

  Their edit may be exactly the thing they want to test, or it may be
  unrelated to the source files they came here to iterate on, so this reports
  it and lets them choose rather than deciding for them.
  """
  conn.out(
    f"{plan.app_id}'s manifest has changed since it was staged:\n"
    f"  source: {plan.source / 'manifest.toml'}\n"
    f"  staged: {plan.run_path / 'happ' / 'manifest.toml'}\n"
    f"Run `harbor stage {plan.app_id}` to update it. Until then this dev run "
    f"uses the staged copy: images, env, ports and mounts are all from it."
  )
  try:
    answer = conn.read("Continue anyway? [y/N] ")
  except EOFError:
    return False
  return answer.strip().lower() in ("y", "yes")


def _receipt(plan: DevPlan) -> str:
  """What is mounted live, and what a dev run deliberately does not do."""
  rows: list[tuple[str, str]] = [("Source:", str(plan.source))]
  for name, path in plan.mounts.items():
    rows.append((f"  {name}:", str(path)))
  if not plan.mounts:
    rows.append(("", "(no app volumes: nothing is mounted from it)"))

  for i, line in enumerate(host_port_lines(plan.stack, plan.run_data)):
    rows.append(("Host:" if i == 0 else "", line))

  # The manifest was read at stage time, so compose.yml is the staged copy's.
  rows.append(("Note:", f"manifest edits need `harbor stage {plan.app_id}`"))
  rows.append(("", "routes are not published for a dev run"))

  width = max(len(label) for label, _ in rows)
  lines = [f"Dev {plan.app_id} (ctrl-c to stop)"]
  lines.extend(f"  {label:<{width}}  {value}" for label, value in rows)
  return "\n".join(lines)
