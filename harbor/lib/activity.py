"""Harbor's own activity output: what an unattended run printed, kept on disk.

There are two log streams and this module owns exactly one of them. Container
runtime logs belong to dockerd -- `harbor logs` streams them through `docker
compose logs`, and copying them here would mean owning rotation and disk-full
for every chatty app. What dockerd cannot keep is what *harbor* did: a job's
`compose run --rm` container is deleted the moment it exits, so harbor's
capture is the only record of what a job or a cron run produced.

Each run leaves one plain file under `$harbor/var/logs/<app_id>/`, readable with
harbor gone -- same no-lock-in rule as the rest of the tree. The structured
index is `activity.logtab`, which already records per-app status: a run adds
an `apps/<app_id>/run` record whose value carries verb, outcome, duration and
the file's relative path. Runs that belong to no app (`fetch`) file under
`harbor/`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harbor.lib.apps import AppID

if TYPE_CHECKING:
  from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.activity")

# Output files kept per directory. The logtab index outlives the files -- jobs
# are not the audit trail, the activity log is -- so pruning loses the text of
# an old run, never the fact of it.
KEEP_RUNS = 50

# Directory for runs that name no app. An app literally named "harbor" would
# share it; both kinds of file are still just runs, so the collision blurs a
# listing rather than corrupting anything.
HARBOR_DIR = "harbor"

# What a run file may be called: the timestamp-verb names `_run_file` builds.
# One path segment, no separators -- the API reads files by this name.
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.log")

OK = "ok"
ERROR = "error"


def _index_key(app_id: AppID | None) -> str:
  return f"apps/{app_id}/run" if app_id else f"{HARBOR_DIR}/run"


def _dirname(app_id: AppID | None) -> str:
  return str(app_id) if app_id else HARBOR_DIR


def _run_file(directory: Path, started: datetime, verb: str) -> Path:
  """A fresh `<timestamp>.<verb>.log` path. Lexical order is time order."""
  stamp = started.strftime("%Y-%m-%dT%H%M%SZ")
  path = directory / f"{stamp}.{verb}.log"
  # Two runs of one verb in one second only happen in tests, but a collision
  # silently appending to the older run's file would be corruption, not noise.
  counter = 2
  while path.exists():
    path = directory / f"{stamp}.{verb}-{counter}.log"
    counter += 1
  return path


def _run_header(
  verb: str,
  app_id: AppID | None,
  started: datetime,
  args: dict[str, str],
) -> str:
  who = f" {app_id}" if app_id else ""
  lines = [
    f"# harbor {verb}{who}",
    f"# started {started.isoformat(timespec='seconds')}",
  ]
  arg_text = " ".join(f"{k}={v}" for k, v in sorted(args.items()))
  if arg_text:
    lines.append(f"# args: {arg_text}")
  return "\n".join(lines) + "\n\n"


def _run_trailer(status: str, finished: datetime, duration_ms: int) -> str:
  return (
    f"# — {status} · finished {finished.isoformat(timespec='seconds')}"
    f" · {duration_ms / 1000:.1f}s\n"
  )


def _new_relpath(
  ctx: HarborCtx, app_id: AppID | None, started: datetime, verb: str
) -> str:
  directory = ctx.config.activity_root / _dirname(app_id)
  directory.mkdir(parents=True, exist_ok=True)
  path = _run_file(directory, started, verb)
  return f"{directory.name}/{path.name}"


def begin_run(
  ctx: HarborCtx,
  verb: str,
  args: dict[str, str],
  *,
  app_id: AppID | None,
  started: datetime,
) -> str:
  """Create the run file so a live job can tee into it. Not indexed yet.

  Returns the path relative to the activity root. Output is appended to this
  file as it happens; `finish_run` appends the closing trailer and adds the
  index row.
  """
  relpath = _new_relpath(ctx, app_id, started, verb)
  path = ctx.config.activity_root / relpath
  path.write_text(_run_header(verb, app_id, started, args))
  return relpath


def finish_run(
  ctx: HarborCtx,
  relpath: str,
  verb: str,
  *,
  app_id: AppID | None,
  status: str,
  started: datetime,
  finished: datetime,
) -> None:
  """Append the closing trailer to ``relpath`` and index the run."""
  path = ctx.config.activity_root / relpath
  path.parent.mkdir(parents=True, exist_ok=True)
  duration_ms = max(0, int((finished - started).total_seconds() * 1000))
  with open(path, "a", encoding="utf-8") as f:
    f.write(_run_trailer(status, finished, duration_ms))
  ctx.activity_log.write(
    _index_key(app_id),
    json.dumps(
      {"verb": verb, "status": status, "ms": duration_ms, "log": relpath},
      separators=(",", ":"),
    ),
  )
  _prune(path.parent)


def record_run(
  ctx: HarborCtx,
  verb: str,
  args: dict[str, str],
  *,
  app_id: AppID | None,
  status: str,
  started: datetime,
  finished: datetime,
  output: str,
) -> str:
  """Write one run's output file and its index record; returns the file's
  path relative to the activity root.

  The file is written first: an index record pointing at a file that never
  made it would be a lie, while a file the index missed is merely unlisted.
  """
  relpath = begin_run(ctx, verb, args, app_id=app_id, started=started)
  if output:
    text = output if output.endswith("\n") else output + "\n"
    with open(ctx.config.activity_root / relpath, "a", encoding="utf-8") as f:
      f.write(text)
  finish_run(
    ctx,
    relpath,
    verb,
    app_id=app_id,
    status=status,
    started=started,
    finished=finished,
  )
  return relpath


def _prune(directory: Path) -> None:
  """Drop the oldest run files once a directory is over budget."""
  files = sorted(p for p in directory.iterdir() if p.suffix == ".log")
  for stale in files[: max(0, len(files) - KEEP_RUNS)]:
    try:
      stale.unlink()
    except OSError as e:  # pragma: no cover - a prune must never fail a run
      logger.warning("could not prune %s: %s", stale, e)


def list_runs(
  ctx: HarborCtx, *, app: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
  """Recorded runs, newest first. `app` narrows to one app's (full) id.

  Read from the index, not the directory: the index remembers runs whose
  output file has since been pruned, and `available` says which is which.
  """
  key = _index_key(AppID(app)) if app and app != HARBOR_DIR else None
  runs: list[dict[str, Any]] = []
  for record_key, entry in ctx.activity_log.history(suffix="/run"):
    if key is not None and record_key != key:
      continue
    if app == HARBOR_DIR and record_key != _index_key(None):
      continue
    try:
      record = json.loads(entry.value)
    except json.JSONDecodeError:
      continue
    relpath = record.get("log", "")
    app_id = record_key.removeprefix("apps/").removesuffix("/run")
    runs.append(
      {
        "ts": entry.ts,
        "app_id": None if record_key == _index_key(None) else app_id,
        "verb": record.get("verb", ""),
        "status": record.get("status", ""),
        "duration_ms": record.get("ms"),
        "log": relpath,
        "available": bool(relpath) and (ctx.config.activity_root / relpath).is_file(),
      }
    )
  runs.reverse()
  return runs[: max(0, limit)]


def read_run_log(ctx: HarborCtx, dirname: str, filename: str) -> str:
  """The text of one run file, named the way the index names it.

  Both parts come from a client, so both are validated as single, known-shape
  path segments before any filesystem contact -- this is the one place the
  admin API's no-paths rule meets a request that has to name a file.
  """
  if dirname != HARBOR_DIR:
    try:
      AppID(dirname)
    except ValueError:
      raise ValueError(f"No activity for {dirname!r}") from None
  if not FILENAME_RE.fullmatch(filename):
    raise ValueError(f"No run log named {filename!r}")
  path = (ctx.config.activity_root / dirname / filename).resolve()
  root = ctx.config.activity_root.resolve()
  if not path.is_relative_to(root) or not path.is_file():
    raise ValueError(f"No run log at {dirname}/{filename}")
  return path.read_text()
