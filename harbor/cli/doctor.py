import argparse

from harbor.lib.harbor import HarborCtx
from harbor.lib.observations import AppObservation


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "doctor", help="Report orphaned or inconsistent Harbor state"
  )
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  problems: list[str] = []
  for observation in ctx.observations():
    for note in _notes(observation):
      problems.append(f"{observation.app_id}: {note}")

  if not problems:
    conn.out("No problems found")
    return

  for problem in problems:
    conn.err(problem)
  raise SystemExit(1)


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
