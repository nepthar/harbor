"""Harbor verbs run as jobs, for callers with no terminal.

A job is what the daemon runs on behalf of a web client: a verb, its
arguments, the text it produced, and how it ended. Jobs exist because the
useful verbs are slow -- `snapshot` copies volumes, `start` can pull images --
and an HTTP request that waits for them dies to a proxy timeout or a page
refresh long before the work does.

Execution is serial by construction. Each verb takes the same locks the CLI
does, so a CLI command and a job cannot write the same app (or harbor-wide
state) at once. A single worker turns "harbor is busy" into a state a caller
can see rather than a lock timeout it has to interpret.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harbor.lib import activity, lifecycle
from harbor.lib.apps import AppID
from harbor.lib.docker import sink_output
from harbor.lib.fetch import (
  GITHUB_PREFIX,
  USAGE,
  FetchResult,
  install_target,
  split_pin,
  update_app,
)
from harbor.lib.happ import is_pathlike
from harbor.lib.harbor import HarborCtx
from harbor.lib.receipt import published_urls

logger = logging.getLogger("harbor.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Enough to show a session's worth of activity without growing forever. Jobs
# are not the audit trail -- the activity log is -- so forgetting old ones
# loses nothing durable.
MAX_HISTORY = 200


def _now() -> str:
  return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
  id: str
  verb: str
  args: dict[str, str]
  state: str = QUEUED
  output: str = ""
  error: str | None = None
  created_at: str = field(default_factory=_now)
  started_at: str | None = None
  finished_at: str | None = None
  # Where the run's output file landed, relative to `$harbor/var/logs`. Jobs are
  # forgotten (restart, MAX_HISTORY); the file is what remains afterwards.
  log: str | None = None

  def as_dict(self) -> dict[str, Any]:
    return {
      "id": self.id,
      "verb": self.verb,
      "args": dict(self.args),
      "state": self.state,
      "output": self.output,
      "error": self.error,
      "created_at": self.created_at,
      "started_at": self.started_at,
      "finished_at": self.finished_at,
      "log": self.log,
    }


@contextmanager
def _capture_harbor_logging(buffer: io.StringIO) -> Iterator[None]:
  """Tee `harbor.*` log records into `buffer` for the duration.

  This is what puts a verb's progress -- the container steps in `rootfs.py`,
  route-provider chatter -- into the job record instead of only on harbord's
  stderr, where no web client will ever see it. The logger is opened up to
  INFO while a job runs; the worker is serial, so the widening never spans two
  jobs.
  """
  handler = logging.StreamHandler(buffer)
  handler.setLevel(logging.INFO)
  handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
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


def _start(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"start {app}", app):
    # Mirrors `harbor start`: prefer the run copy once staged, so an app whose
    # catalog entry has since been deleted still starts.
    bundle = (
      ctx.config.app_run_path(app) if ctx.is_staged(app) else ctx.bundle_path(app)
    )
    result = lifecycle.start(app, bundle, ctx)
    lines = [f"Started {app}"]
    lines += [f"  {url}" for url in published_urls(result.stack, result.run_data, ctx)]
    return "\n".join(lines)


def _stop(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"stop {app}", app):
    lifecycle.stop(app, ctx)
  return f"Stopped {app}"


def _stage(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"stage {app}", app):
    result = lifecycle.stage(app, ctx.bundle_path(app), ctx)
    lines = [f"Staged {app} at {ctx.run_path(app)}"]
    lines += [
      f"  volume {name} is no longer declared in the manifest; its link is gone "
      f"but its data was left in place"
      for name in result.dropped_volumes
    ]
    return "\n".join(lines)


def _fetch(ctx: HarborCtx, args: dict[str, str]) -> str:
  """`harbor fetch <target>`, with the prompt replaced by an explicit `yes`.

  The CLI asks before a first install, because harbor cannot vouch for a happ's
  author and the receipt is the only check there is. A job cannot be asked
  anything, so the caller has to have decided already -- a client that wants
  the operator to see what they are approving shows them the preview first.
  An update carries no prompt in the CLI either, so it needs no `yes` here.
  """
  with ctx.harbor_lock(f"fetch {args['target']}"):
    spec, pin = split_pin(args["target"])
    if spec.startswith(GITHUB_PREFIX):
      if args.get("yes") not in ("1", "true", "yes"):
        raise ValueError(
          f"Fetching {spec} installs code harbor cannot vouch for. Read its "
          f"manifest first, then resubmit with yes=1 to confirm."
        )
      result = install_target(spec, pin, ctx)
      return f"Installed {result.app_id} at {result.path}"

    if is_pathlike(spec):
      raise ValueError(
        f"Don't know how to fetch {args['target']!r}; expected a github: target "
        f"or an installed app id.\n  {USAGE}"
      )
    if "@" in args["target"] and pin is None:
      raise ValueError(
        f"Pin must be a full 40-character commit sha, not "
        f"{args['target'].rsplit('@', 1)[-1]!r}"
      )

    app = ctx.resolve_app(spec)
    result = update_app(app, pin, ctx)
    if not isinstance(result, FetchResult):
      return result
    lines = [f"Updated {app}", f" - {result.previous}", f" + {result.current}"]
    if result.staged:
      lines.append(f"Re-stage and restart to pick it up: harbor stage {app}")
    return "\n".join(lines)


def _snapshot(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  path = lifecycle.take_snapshot(app, ctx, label=args.get("label", ""))
  return f"Snapshot of {app} written to {path}"


def _cmd(ctx: HarborCtx, args: dict[str, str]) -> str:
  """Run a manifest `[commands]` entry, exactly what `harbor cmd` does.

  `command` names an entry the happ's manifest already declares -- an id of a
  thing that exists, like every other verb's argument. The argv it runs is the
  manifest's, not the caller's, so this grants the operator nothing their happ
  did not already define. Any output the command produced is captured into the
  job (and its activity file) by the runner; a non-zero exit fails the job so
  it reads as an error rather than a silent success.
  """
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"cmd {app}", app):
    code = lifecycle.run_command(app, args["command"], [], ctx)
    if code != 0:
      raise ValueError(
        f"Command {args['command']!r} for {app} exited with status {code}"
      )
    return f"Ran command {args['command']!r} for {app}"


@dataclass(frozen=True)
class Verb:
  run: Callable[[HarborCtx, dict[str, str]], str]
  required: tuple[str, ...] = ("app",)
  optional: tuple[str, ...] = ()


# Every verb here but `fetch` takes ids of things that already exist -- an app
# id, or a `command` name the app's own manifest declares. Nothing accepts a
# manifest or a filesystem path: those are the arguments that let a caller
# define what an app *is*, and defining an app means arbitrary bind mounts,
# which means root. Path-style `stage` stays CLI-only for exactly that reason.
# `fetch` takes a github: URL and nothing else -- `parse_target` refuses
# anything that is not one -- and it only copies files into the apps root;
# staging and starting stay separate, deliberate steps.
VERBS: dict[str, Verb] = {
  "start": Verb(_start),
  "stop": Verb(_stop),
  "stage": Verb(_stage),
  "snapshot": Verb(_snapshot, optional=("label",)),
  # Runs an argv the manifest already declares. See `_cmd`: the caller names
  # which command, never what it does.
  "cmd": Verb(_cmd, required=("app", "command")),
  # The one verb that takes a URL rather than an id. See the note above: it is
  # here because the operator asked for it from the web UI, and it is confined
  # to what `harbor fetch` already does -- copy a happ into the apps root. It
  # neither stages nor starts, so nothing it fetches runs until someone asks.
  "fetch": Verb(_fetch, required=("target",), optional=("yes",)),
}


def validate(verb: str, args: dict[str, str], ctx: HarborCtx) -> None:
  """Refuse a submission the runner would only fail on later.

  The app is resolved here so an unknown id is a rejected request rather than
  a job that exists solely to report that it could not start.
  """
  spec = VERBS.get(verb)
  if spec is None:
    known = ", ".join(sorted(VERBS))
    raise ValueError(f"Unknown verb {verb!r}; known verbs are: {known}")

  allowed = set(spec.required) | set(spec.optional)
  for name in args:
    if name not in allowed:
      raise ValueError(
        f"Verb {verb!r} takes no argument {name!r}; "
        f"it accepts: {', '.join(sorted(allowed))}"
      )
  for name in spec.required:
    if not args.get(name):
      raise ValueError(f"Verb {verb!r} requires argument {name!r}")

  # Only for the verbs that name an app. `fetch` takes a target that may not
  # resolve to anything yet -- that is the whole point of fetching it.
  if "app" in spec.required:
    ctx.resolve_app(args["app"])


class JobRunner:
  """Holds the job history and runs one job at a time.

  A worker is optional: with no thread started, `run_pending` drains the queue
  on the calling thread, which is how the tests exercise jobs without waiting
  on one.
  """

  def __init__(self, ctx_factory: Callable[[], HarborCtx]) -> None:
    self._ctx_factory = ctx_factory
    self._jobs: dict[str, Job] = {}
    self._queue: queue.Queue[str] = queue.Queue()
    self._lock = threading.Lock()
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None:
      raise RuntimeError("JobRunner is already running")
    self._thread = threading.Thread(target=self._work, name="harbor-jobs", daemon=True)
    self._thread.start()

  def submit(self, verb: str, args: dict[str, str]) -> dict[str, Any]:
    job = Job(id=uuid.uuid4().hex[:12], verb=verb, args=dict(args))
    with self._lock:
      self._jobs[job.id] = job
      self._trim()
      snapshot = job.as_dict()
    self._queue.put(job.id)
    return snapshot

  def get(self, job_id: str) -> dict[str, Any] | None:
    with self._lock:
      job = self._jobs.get(job_id)
      return job.as_dict() if job else None

  def list(self) -> list[dict[str, Any]]:
    with self._lock:
      return [job.as_dict() for job in reversed(list(self._jobs.values()))]

  def run_pending(self) -> int:
    """Run every queued job on this thread. Returns how many ran."""
    count = 0
    while True:
      try:
        job_id = self._queue.get_nowait()
      except queue.Empty:
        return count
      self._run(job_id)
      count += 1

  def _work(self) -> None:
    while True:
      self._run(self._queue.get())

  def _run(self, job_id: str) -> None:
    with self._lock:
      job = self._jobs.get(job_id)
      if job is None:
        return
      job.state = RUNNING
      job.started_at = _now()
      verb, args = job.verb, dict(job.args)

    # A fresh context per job, exactly as a CLI invocation would build one:
    # nothing a job sees is carried over from the job before it.
    started = datetime.now(UTC)
    captured = io.StringIO()
    ctx = None
    try:
      ctx = self._ctx_factory()
      with (
        _capture_harbor_logging(captured),
        sink_output(captured),
      ):
        output, error = VERBS[verb].run(ctx, args), None
    except (ValueError, RuntimeError) as e:
      output, error = "", str(e)
    except Exception as e:  # noqa: BLE001 - a crashed worker would strand every later job
      logger.exception("job %s (%s) raised", job_id, verb)
      output, error = "", f"{type(e).__name__}: {e}"

    finished = datetime.now(UTC)
    # Progress first, the verb's own summary last -- the order a terminal
    # would have shown them in.
    output = "\n".join(part for part in (captured.getvalue().rstrip(), output) if part)
    log_path = self._record(ctx, verb, args, error, started, finished, output)

    with self._lock:
      job.state = FAILED if error else DONE
      job.output = output
      job.error = error
      job.finished_at = _now()
      job.log = log_path

  def _record(
    self,
    ctx: HarborCtx | None,
    verb: str,
    args: dict[str, str],
    error: str | None,
    started: datetime,
    finished: datetime,
    output: str,
  ) -> str | None:
    """File the run under `$harbor/var/logs`. Never fails the job over it."""
    if ctx is None:
      return None
    try:
      return activity.record_run(
        ctx.config,
        verb,
        args,
        app_id=self._record_app(ctx, args),
        status=activity.ERROR if error else activity.OK,
        started=started,
        finished=finished,
        output="\n".join(part for part in (output, error) if part),
      )
    except Exception:  # noqa: BLE001 - the job itself already succeeded or failed on its own terms
      logger.exception("could not record activity for `%s`", verb)
      return None

  @staticmethod
  def _record_app(ctx: HarborCtx, args: dict[str, str]) -> AppID | None:
    """Which app directory the run files under, resolving stems to full ids.

    Best effort: a job can fail precisely because its app does not resolve,
    and that run still deserves a record -- under `harbor/` if need be.
    """
    query = args.get("app", "")
    if not query:
      return None
    try:
      return ctx.resolve_app(query)
    except (ValueError, RuntimeError):
      try:
        return AppID(query)
      except ValueError:
        return None

  def _trim(self) -> None:
    """Drop the oldest finished jobs once the history is over budget."""
    if len(self._jobs) <= MAX_HISTORY:
      return
    for job_id in list(self._jobs):
      if len(self._jobs) <= MAX_HISTORY:
        return
      if self._jobs[job_id].state in (DONE, FAILED):
        del self._jobs[job_id]
