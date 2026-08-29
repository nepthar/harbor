"""Serial execution of Jobs, for callers with no terminal.

The queue exists because the useful verbs are slow and an HTTP request that
waits for them dies long before the work does. Execution is serial by
construction: each job takes the same locks the CLI does.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import schedule

from harbor.jobs.cmd import CmdJob
from harbor.jobs.install import InstallJob
from harbor.jobs.job import DONE, FAILED, QUEUED, RUNNING, Job
from harbor.jobs.metrics import HostMetricsJob, VolumeMetricsJob
from harbor.jobs.remove import ResetJob, UninstallJob
from harbor.jobs.restart import RestartJob
from harbor.jobs.restore import RestoreJob
from harbor.jobs.snapshot import SnapshotJob
from harbor.jobs.start import StartJob
from harbor.jobs.stop import StopJob
from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.jobs")

MAX_HISTORY = 200

# Every verb here takes ids of things that already exist -- an app id, or a
# `command` name the app's own manifest declares. Nothing accepts a manifest or
# a filesystem path: those are the arguments that let a caller define what an
# app *is*, and defining an app means arbitrary bind mounts, which means root.
# Path-style `stage` stays CLI-only for exactly that reason.
JOBS: dict[str, type[Job]] = {
  "start": StartJob,
  "stop": StopJob,
  "restart": RestartJob,
  "install": InstallJob,
  "snapshot": SnapshotJob,
  "restore": RestoreJob,
  "cmd": CmdJob,
  "uninstall": UninstallJob,
  "reset": ResetJob,
  "volume-metrics": VolumeMetricsJob,
  "host-metrics": HostMetricsJob,
}


def metric_schedule(submit: Callable[[str], None]) -> schedule.Scheduler:
  """host-metrics every 5 minutes, volume-metrics every hour. In-process only."""
  sched = schedule.Scheduler()
  sched.every(5).minutes.do(submit, "host-metrics")
  sched.every().hour.do(submit, "volume-metrics")
  return sched


class JobRunner:
  """Holds the job history and runs one at a time."""

  def __init__(self, ctx_factory: Callable[[], HarborCtx]) -> None:
    self._ctx_factory = ctx_factory
    self._jobs: dict[str, Job] = {}
    self._queue: queue.Queue[str] = queue.Queue()
    self._lock = threading.Lock()
    self._thread: threading.Thread | None = None
    self._sched: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None:
      raise RuntimeError("JobRunner is already running")
    self._thread = threading.Thread(target=self._work, name="harbor-jobs", daemon=True)
    self._thread.start()
    self._sched = threading.Thread(
      target=self._schedule, name="harbor-jobs-sched", daemon=True
    )
    self._sched.start()

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
    """Run every queued job on this thread; returns how many ran.

    Only for a runner whose worker was never started -- two threads draining one
    queue would break the serial execution jobs are written against.
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

  def _schedule(self) -> None:
    sched = metric_schedule(self._submit_scheduled)
    while True:
      sched.run_pending()
      time.sleep(1)

  def _submit_scheduled(self, verb: str) -> None:
    if self._busy_with(verb):
      return
    try:
      self.submit(verb, {}, self._ctx_factory())
    except Exception:  # noqa: BLE001 - a missed tick must not kill the scheduler
      logger.exception("could not submit scheduled %s", verb)

  def _busy_with(self, verb: str) -> bool:
    with self._lock:
      return any(
        job.name == verb and job.state in (QUEUED, RUNNING)
        for job in self._jobs.values()
      )

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
