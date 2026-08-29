import argparse

from harbor.lib.harbor import HarborCtx, ambiguity_message
from harbor.lib.observations import AppObservation


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "doctor", help="Report orphaned or inconsistent Harbor state"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  with ctx.harbor_lock("doctor"):
    problems: list[str] = list(_catalog_notes(ctx))
    for observation in ctx.observations():
      for note in _notes(observation):
        problems.append(f"{observation.app_id}: {note}")

    if not problems:
      conn.out("No problems found")
      return

    for problem in problems:
      conn.err(problem)
    raise SystemExit(1)


def _catalog_notes(ctx: HarborCtx) -> list[str]:
  """Problems with the catalog itself, rather than with any one app's state."""
  notes = []
  for name, repo in ctx.config.repos.items():
    if not repo.path.is_dir():
      hint = (
        f"Run `harbor repo update {name}` to mirror it."
        if repo.mirrored
        else "Create it, fix its path in config.toml, or drop the entry."
      )
      notes.append(f"repo {name}: {repo.path} is not a directory. {hint}")

  catalog = ctx.app_catalog()
  for app_id in sorted(catalog):
    entries = catalog[app_id]
    if len(entries) > 1:
      notes.append(ambiguity_message(app_id, entries))
  return notes


def _notes(observation: AppObservation) -> tuple[str, ...]:
  notes = []
  if observation.bundle_path is None and (
    observation.run_dir_exists or observation.containers or observation.db_present
  ):
    notes.append("app bundle missing")
  if not observation.run_dir_exists and observation.containers:
    notes.append("run directory missing")
  elif observation.run_dir_exists and not observation.compose_exists:
    notes.append("compose missing")
  if observation.containers and not observation.compose_exists:
    notes.append("manual container recovery required")
  if any(not container.run_unit for container in observation.containers):
    notes.append("container run-unit label missing")
  if 0 < observation.running_count < len(observation.containers):
    notes.append("mixed container states")
  if (
    observation.db_present
    and observation.bundle_path is None
    and not observation.run_dir_exists
    and not observation.containers
  ):
    notes.append("orphaned route allocation")
  return tuple(notes)
