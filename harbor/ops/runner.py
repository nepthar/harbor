"""Serial execution of Operations, for callers with no terminal.

An Operation is what the daemon runs on behalf of a web client: a verb, its
arguments, the text it produced, and how it ended. Ops exist because the
useful verbs are slow -- `snapshot` copies volumes, `start` can pull images --
and an HTTP request that waits for them dies to a proxy timeout or a page
refresh long before the work does.

Execution is serial by construction. Each op takes the same locks the CLI
does, so a CLI command and a queued op cannot write the same app (or
harbor-wide state) at once. A single worker turns "harbor is busy" into a
state a caller can see rather than a lock timeout it has to interpret.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from harbor.lib.harbor import HarborCtx
from harbor.ops.cmd import CmdOp
from harbor.ops.fetch import FetchOp
from harbor.ops.operation import DONE, FAILED, BaseOp
from harbor.ops.restore import RestoreOp
from harbor.ops.snapshot import SnapshotOp
from harbor.ops.stage import StageOp
from harbor.ops.start import StartOp
from harbor.ops.stop import StopOp

logger = logging.getLogger("harbor.ops")

# Enough to show a session's worth of activity without growing forever. Ops
# are not the audit trail -- the activity log is -- so forgetting old ones
# loses nothing durable.
MAX_HISTORY = 200

# Every verb here but `fetch` takes ids of things that already exist -- an app
# id, or a `command` name the app's own manifest declares. Nothing accepts a
# manifest or a filesystem path: those are the arguments that let a caller
# define what an app *is*, and defining an app means arbitrary bind mounts,
# which means root. Path-style `stage` stays CLI-only for exactly that reason.
# `fetch` takes a github: URL and nothing else -- `parse_target` refuses
# anything that is not one -- and it only copies files into the apps root;
# staging and starting stay separate, deliberate steps.
OPS: dict[str, type[BaseOp]] = {
  "start": StartOp,
  "stop": StopOp,
  "stage": StageOp,
  "snapshot": SnapshotOp,
  "restore": RestoreOp,
  "cmd": CmdOp,
  "fetch": FetchOp,
}


class JobRunner:
  """Holds the op history and runs one at a time.

  A worker is optional: with no thread started, `run_pending` drains the queue
  on the calling thread, which is how the tests exercise ops without waiting
  on one.
  """

  def __init__(self, ctx_factory: Callable[[], HarborCtx]) -> None:
    self._ctx_factory = ctx_factory
    self._ops: dict[str, BaseOp] = {}
    self._queue: queue.Queue[str] = queue.Queue()
    self._lock = threading.Lock()
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None:
      raise RuntimeError("JobRunner is already running")
    self._thread = threading.Thread(target=self._work, name="harbor-jobs", daemon=True)
    self._thread.start()

  def submit(self, verb: str, args: dict[str, str], ctx: HarborCtx) -> dict[str, Any]:
    spec = OPS.get(verb)
    if spec is None:
      known = ", ".join(sorted(OPS))
      raise ValueError(f"Unknown verb {verb!r}; known verbs are: {known}")
    op = spec()
    op.args = dict(args)
    spec._check_args(args)
    op.init(ctx, args)
    with self._lock:
      self._ops[op.id] = op
      self._trim()
      payload = op.as_dict()
    self._queue.put(op.id)
    return payload

  def get(self, job_id: str) -> dict[str, Any] | None:
    with self._lock:
      op = self._ops.get(job_id)
      return op.as_dict() if op else None

  def list(self) -> list[dict[str, Any]]:
    with self._lock:
      return [op.as_dict() for op in reversed(list(self._ops.values()))]

  def run_pending(self) -> int:
    """Run every queued op on this thread. Returns how many ran."""
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
      op = self._ops.get(job_id)
      if op is None:
        return
    # A fresh context per op, exactly as a CLI invocation would build one:
    # nothing an op sees is carried over from the one before it.
    try:
      op.execute(self._ctx_factory())
    except (ValueError, RuntimeError):
      pass
    except Exception:  # noqa: BLE001 - a crashed worker would strand every later op
      logger.exception("op %s (%s) raised", job_id, op.name)
      if op.state != FAILED:
        op.state = FAILED
        op.error = op.error or "operation failed"

  def _trim(self) -> None:
    """Drop the oldest finished ops once the history is over budget."""
    if len(self._ops) <= MAX_HISTORY:
      return
    for job_id in list(self._ops):
      if len(self._ops) <= MAX_HISTORY:
        return
      if self._ops[job_id].state in (DONE, FAILED):
        del self._ops[job_id]
