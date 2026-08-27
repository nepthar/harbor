"""Harbor's own activity output: what an unattended run printed, kept on disk.

There are two log streams and this module owns exactly one of them. Container
runtime logs belong to dockerd -- `harbor logs` streams them through `docker
compose logs`, and copying them here would mean owning rotation and disk-full
for every chatty app. What dockerd cannot keep is what *harbor* did: a job's
`compose run --rm` container is deleted the moment it exits, so harbor's
capture is the only record of what a job or a cron run produced.

Each run leaves one plain file under `$harbor/var/logs/`, named
`{timestamp}.{app_id}.{verb}.log` (the app id is omitted for runs that
belong to no app). Readable with harbor gone -- same no-lock-in rule as
the rest of the tree. The structured index is `activity.logtab`, which
already records per-app status: a run adds an `apps/<app_id>/run` record
whose value carries verb, outcome, duration and the file's name. Runs that
belong to no app (`fetch`) index under `harbor/`.

`Activity` is the context manager that ties the two together: it opens the
file, points harbor's logging and streamed subprocess output at it, and
files the index row on the way out. Everything that records a run goes
through it -- `harbor.jobs` wraps it, and any code with a block of work
worth remembering can use it directly.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from harbor.lib.apps import AppID
from harbor.lib.docker import sink_output

if TYPE_CHECKING:
  from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.activity")

# Output files kept. The logtab index outlives the files -- jobs are not
# the audit trail, the activity log is -- so pruning loses the text of an
# old run, never the fact of it.
KEEP_RUNS = 50

# Index key for runs that name no app. Distinct from `apps/harbor/run`,
# which would belong to an app literally named "harbor".
HARBOR_DIR = "harbor"

# What a run file may be called: the timestamp-verb names `_run_file` builds.
# One path segment, no separators -- the API reads files by this name.
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.log")

OK = "ok"
ERROR = "error"


def _index_key(app_id: AppID | None) -> str:
  return f"apps/{app_id}/run" if app_id else f"{HARBOR_DIR}/run"


def _run_file(
  directory: Path, started: datetime, verb: str, app_id: AppID | None
) -> Path:
  """A fresh `{timestamp}.{app_id}.{verb}.log` path. Lexical order is time order."""
  stamp = started.strftime("%Y-%m-%dT%H%M%SZ")
  middle = f"{app_id}." if app_id else ""
  path = directory / f"{stamp}.{middle}{verb}.log"
  # Two runs of one verb in one second only happen in tests, but a collision
  # silently appending to the older run's file would be corruption, not noise.
  counter = 2
  while path.exists():
    path = directory / f"{stamp}.{middle}{verb}-{counter}.log"
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
  directory = ctx.config.activity_root
  directory.mkdir(parents=True, exist_ok=True)
  return _run_file(directory, started, verb, app_id).name


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


@contextmanager
def _capture_harbor_logging(stream: io.TextIOBase) -> Iterator[None]:
  """Tee `harbor.*` log records into `stream` for the duration.

  Progress that library code writes through `logging` -- container steps,
  route-provider chatter -- has to land in the run log, not only on this
  process's stderr. The logger is opened up to INFO while the block runs;
  runs are serial, so the widening never spans two of them.
  """
  handler = logging.StreamHandler(stream)
  handler.setLevel(logging.INFO)
  # UTC with a Z, like the header and trailer lines around it.
  formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%SZ"
  )
  formatter.converter = time.gmtime
  handler.setFormatter(formatter)
  harbor_logger = logging.getLogger("harbor")
  previous_level = harbor_logger.level
  if harbor_logger.getEffectiveLevel() > logging.INFO:
    harbor_logger.setLevel(logging.INFO)
  harbor_logger.addHandler(handler)
  try:
    yield
  finally:
    harbor_logger.removeHandler(handler)
    harbor_logger.setLevel(previous_level)


class _Sink(io.TextIOBase):
  """The run's log file, flushed per write so a reader can tail it mid-run.

  `echo` is the attended case: the same bytes also go to the operator's
  terminal.
  """

  def __init__(self, log: io.TextIOBase, echo: TextIO | None = None) -> None:
    self._log = log
    self._echo = echo

  def write(self, s: str) -> int:
    self._log.write(s)
    self._log.flush()
    if self._echo is not None:
      self._echo.write(s)
      self._echo.flush()
    return len(s)

  def flush(self) -> None:
    self._log.flush()
    if self._echo is not None:
      self._echo.flush()


class Activity:
  """One recorded run of harbor's own work.

      with Activity(ctx, "refresh", app=app) as act:
        stop(app, ctx)
        stage(app, bundle, ctx)
        start(app, bundle, ctx)

  Inside the block, `harbor.*` log records and streamed docker output land in
  the run's file under `$harbor/var/logs` as they happen, so a reader tailing
  that file sees progress. `echo` copies the same bytes to a second stream --
  a terminal, when the caller has one. On the way out the file gets its
  closing trailer and the run is indexed, whether the block returned or
  raised; the exception is recorded and then propagates.

  The block is the unit of recording, so keep it to the work itself: resolve
  and validate arguments *before* entering. A run that could never have
  started leaves no log to explain, which is the point.

  One block is one row in the activity index, so do not nest them. Work that
  belongs to a larger action is a function the block calls, not a block of
  its own.
  """

  def __init__(
    self,
    ctx: HarborCtx,
    verb: str,
    *,
    app: AppID | str | None = None,
    args: dict[str, str] | None = None,
    echo: TextIO | None = None,
  ) -> None:
    self.ctx = ctx
    self.verb = verb
    self.app = _as_app_id(app)
    self.args = dict(args or {})
    self.echo = echo
    # Set once the file exists; `None` means the run never got that far.
    self.log: str | None = None
    self.error: str | None = None
    self._started: datetime | None = None
    self._file: TextIO | None = None
    self._sink: _Sink | None = None
    self._stack: ExitStack | None = None

  def __enter__(self) -> Activity:
    self._started = datetime.now(UTC)
    self.log = begin_run(
      self.ctx, self.verb, self.args, app_id=self.app, started=self._started
    )
    self._file = open(  # noqa: SIM115 - closed in __exit__
      self.ctx.config.activity_root / self.log, "a", encoding="utf-8", buffering=1
    )
    self._sink = _Sink(self._file, self.echo)
    self._stack = ExitStack()
    self._stack.enter_context(_capture_harbor_logging(self._sink))
    self._stack.enter_context(sink_output(self._sink))
    return self

  def __exit__(self, exc_type, exc, tb) -> None:
    if self._stack is not None:
      self._stack.close()
    if exc is not None:
      self.error = _describe(exc)
    if self._file is not None:
      # The error goes to the file only; an attended caller prints it to the
      # terminal itself on the way out.
      if self.error is not None:
        self._file.write(f"Error: {self.error}\n")
      self._file.close()
    self._sink = None
    self._file = None
    self._stack = None
    if self.log is not None and self._started is not None:
      try:
        finish_run(
          self.ctx,
          self.log,
          self.verb,
          app_id=self.app,
          status=OK if exc is None else ERROR,
          started=self._started,
          finished=datetime.now(UTC),
        )
      except Exception:  # noqa: BLE001 - a log failure must not hide the block's own error
        logger.exception("could not record activity for `%s`", self.verb)

  def write(self, text: str) -> None:
    """Put `text` in the run log (and the echo stream) verbatim."""
    if self._sink is None:
      raise RuntimeError(f"{self.verb} activity is not running")
    self._sink.write(text)

  def subprocess(
    self,
    cmd: list[str],
    *,
    parse_json: bool = False,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
  ) -> Any:
    """Run `cmd`. JSON out is captured and parsed; anything else is streamed.

    Streamed output goes to the run log (and the echo stream, when there is
    one) as the child writes, so a reader tailing the log sees progress. A
    non-zero exit is a `RuntimeError` naming the command.
    """
    if self._sink is None:
      raise RuntimeError(f"{self.verb} activity is not running")
    run_env = {**os.environ, **env} if env else None
    if parse_json:
      result = subprocess.run(cmd, capture_output=True, text=True, env=run_env, cwd=cwd)
      if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
          f"Command {cmd[0]!r} exited with status {result.returncode}"
          + (f": {detail}" if detail else "")
        )
      try:
        return json.loads(result.stdout)
      except ValueError as e:
        raise RuntimeError(f"Command {cmd[0]!r} did not return JSON: {e}") from e

    proc = subprocess.Popen(
      cmd,
      cwd=cwd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      env=run_env,
    )
    stdout = proc.stdout
    assert stdout is not None
    try:
      while True:
        chunk = stdout.read(4096)
        if not chunk:
          break
        self._sink.write(chunk)
    finally:
      stdout.close()
      code = proc.wait()
    if code != 0:
      raise RuntimeError(f"Command {cmd[0]!r} exited with status {code}")
    return None


def _as_app_id(app: AppID | str | None) -> AppID | None:
  """An id the log can file under, or `None` for the harbor-wide directory."""
  if not app:
    return None
  if isinstance(app, AppID):
    return app
  try:
    return AppID(app)
  except ValueError:
    return None


def _describe(exc: BaseException) -> str:
  """How an exception reads in a run log and a job record."""
  if isinstance(exc, ValueError | RuntimeError):
    return str(exc)
  return f"{type(exc).__name__}: {exc}"


def _prune(directory: Path) -> None:
  """Drop the oldest run files once the activity root is over budget."""
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


def read_run_log(ctx: HarborCtx, filename: str) -> str:
  """The text of one run file, named the way the index names it.

  The name comes from a client, so it is validated as a single, known-shape
  path segment before any filesystem contact -- this is the one place the
  admin API's no-paths rule meets a request that has to name a file.
  """
  if not FILENAME_RE.fullmatch(filename):
    raise ValueError(f"No run log named {filename!r}")
  path = (ctx.config.activity_root / filename).resolve()
  root = ctx.config.activity_root.resolve()
  if not path.is_relative_to(root) or not path.is_file():
    raise ValueError(f"No run log named {filename!r}")
  return path.read_text()
