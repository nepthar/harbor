"""Serial execution of Jobs, for callers with no terminal.

A queued job is what the daemon runs on behalf of a web client: a verb, its
arguments, and how it ended. The queue exists because the useful verbs are
slow -- `snapshot` copies volumes, `start` can pull images -- and an HTTP
request that waits for them dies to a proxy timeout or a page refresh long
before the work does.

Execution is serial by construction. Each job takes the same locks the CLI
does, so a CLI command and a queued job cannot write the same app (or
harbor-wide state) at once. A single worker turns "harbor is busy" into a
state a caller can see rather than a lock timeout it has to interpret.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from harbor.jobs.cmd import CmdJob
from harbor.jobs.fetch import FetchJob
from harbor.jobs.job import DONE, FAILED, Job
from harbor.jobs.restore import RestoreJob
from harbor.jobs.snapshot import SnapshotJob
from harbor.jobs.stage import StageJob
from harbor.jobs.start import StartJob
from harbor.jobs.stop import StopJob
from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.jobs")

# Enough to show a session's worth of activity without growing forever. The
# runner's history is not the audit trail -- the activity log is -- so
# forgetting old jobs loses nothing durable.
MAX_HISTORY = 200

# Every verb here but `fetch` takes ids of things that already exist -- an app
# id, or a `command` name the app's own manifest declares. Nothing accepts a
# manifest or a filesystem path: those are the arguments that let a caller
# define what an app *is*, and defining an app means arbitrary bind mounts,
# which means root. Path-style `stage` stays CLI-only for exactly that reason.
# `fetch` takes a github: URL and nothing else -- `parse_target` refuses
# anything that is not one -- and it only copies files into the apps root;
# staging and starting stay separate, deliberate steps.
JOBS: dict[str, type[Job]] = {
  "start": StartJob,
  "stop": StopJob,
  "stage": StageJob,
  "snapshot": SnapshotJob,
  "restore": RestoreJob,
  "cmd": CmdJob,
  "fetch": FetchJob,
}


class JobRunner:
  """Holds the job history and runs one at a time.

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

  def submit(self, verb: str, args: dict[str, str], ctx: HarborCtx) -> dict[str, Any]:
    spec = JOBS.get(verb)
    if spec is None:
      known = ", ".join(sorted(JOBS))
      raise ValueError(f"Unknown verb {verb!r}; known verbs are: {known}")
    job = spec.prepare(args, ctx)
    with self._lock:
      self._jobs[job.id] = job
      self._trim()
      payload = job.as_dict()
    self._queue.put(job.id)
    return payload

  def get(self, job_id: str) -> dict[str, Any] | None:
    with self._lock:
      job = self._jobs.get(job_id)
      return job.as_dict() if job else None

  def list(self) -> list[dict[str, Any]]:
    with self._lock:
      return [job.as_dict() for job in reversed(list(self._jobs.values()))]

  def run_pending(self) -> int:
    """Run every queued job on this thread. Returns how many ran.

    Only for a runner whose worker was never started: two threads draining
    one queue would break the everything-is-serial invariant the jobs
    (logging capture, lock ordering) are written against.
    """
    if self._thread is not None:
      raise RuntimeError("JobRunner has a worker thread; it drains the queue")
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
    # A fresh context per job, exactly as a CLI invocation would build one:
    # nothing a job sees is carried over from the one before it.
    try:
      job.execute(self._ctx_factory())
    except (ValueError, RuntimeError):
      pass
    except Exception:  # noqa: BLE001 - a crashed worker would strand every later job
      logger.exception("job %s (%s) raised", job_id, job.name)
      if job.state != FAILED:
        job.state = FAILED
        job.error = job.error or "job failed"

  def _trim(self) -> None:
    """Drop the oldest finished jobs once the history is over budget."""
    if len(self._jobs) <= MAX_HISTORY:
      return
    for job_id in list(self._jobs):
      if len(self._jobs) <= MAX_HISTORY:
        return
      if self._jobs[job_id].state in (DONE, FAILED):
        del self._jobs[job_id]
