"""A harbor verb the daemon can be asked for over the wire.

A Job adapts a request -- a verb name and a flat dict of string arguments --
to the work itself. The recording is not this module's job: `execute` wraps
`run` in an `Activity`, which owns the log file and the index row. Code that
wants a run recorded but has no wire to answer to uses `Activity` directly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, TextIO

from harbor.lib.activity import Activity
from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def _now() -> str:
  return datetime.now(UTC).isoformat(timespec="seconds")


class Job:
  name: str
  description: str
  required_args: tuple[str, ...] = ()
  optional_args: tuple[str, ...] = ()
  record_activity: bool = True
  app: str | None = None

  def __init__(self) -> None:
    self.id: str = uuid.uuid4().hex[:12]
    self.args: dict[str, str] = {}
    self.state: str = QUEUED
    self.error: str | None = None
    self.created_at: str = _now()
    self.started_at: str | None = None
    self.finished_at: str | None = None
    self.log: str | None = None
    self._activity: Activity | None = None

  def as_dict(self) -> dict[str, Any]:
    return {
      "id": self.id,
      "verb": self.name,
      "args": dict(self.args),
      "state": self.state,
      "error": self.error,
      "created_at": self.created_at,
      "started_at": self.started_at,
      "finished_at": self.finished_at,
      "log": self.log,
    }

  @classmethod
  def prepare(cls, args: dict[str, str], ctx: HarborCtx) -> Job:
    """Construct and parse. Raises ``ValueError`` before any log is filed."""
    job = cls()
    job.args = dict(args)
    cls._check_args(args)
    job.init(ctx, args)
    return job

  @classmethod
  def call(
    cls, args: dict[str, str], ctx: HarborCtx, *, echo: TextIO | None = None
  ) -> Job:
    """Parse, execute, re-raise on failure. Returns the finished job on success."""
    job = cls.prepare(args, ctx)
    job.execute(ctx, echo=echo)
    return job

  def execute(self, ctx: HarborCtx, *, echo: TextIO | None = None) -> None:
    """Run the work, optionally as one recorded Activity, then re-raise."""
    self.state = RUNNING
    self.started_at = _now()
    if not self.record_activity:
      self._execute_quiet(ctx)
      return
    activity = Activity(ctx, self.name, app=self.app, args=self.args, echo=echo)
    ok = False
    try:
      with activity:
        self._activity = activity
        self.log = activity.log
        self.run(ctx)
      ok = True
    finally:
      self._activity = None
      self.log = activity.log
      self.error = activity.error
      self.state = DONE if ok else FAILED
      self.finished_at = _now()

  def _execute_quiet(self, ctx: HarborCtx) -> None:
    ok = False
    try:
      self.run(ctx)
      ok = True
    except Exception as e:
      self.error = str(e)
      raise
    finally:
      self.state = DONE if ok else FAILED
      self.finished_at = _now()

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    """Parse arguments that already passed `required_args` / `optional_args`.

    Raises ValueError if they are unusable; `execute` then never runs and no
    log is written.
    """

  def run(self, ctx: HarborCtx) -> None:
    raise NotImplementedError

  def subprocess(self, cmd: list[str], **kwargs: Any) -> Any:
    """Run ``cmd`` inside this job's Activity. See ``Activity.subprocess``."""
    if self._activity is None:
      raise RuntimeError(
        f"{self.name} subprocess is only available while the job is running"
      )
    return self._activity.subprocess(cmd, **kwargs)

  @classmethod
  def _check_args(cls, args: dict[str, str]) -> None:
    """Refuse unknown or missing keys against ``required_args`` / ``optional_args``."""
    allowed = set(cls.required_args) | set(cls.optional_args)
    for name in args:
      if name not in allowed:
        names = ", ".join(sorted(allowed)) if allowed else "(none)"
        raise ValueError(
          f"Verb {cls.name!r} takes no argument {name!r}; it accepts: {names}"
        )
    for name in cls.required_args:
      if not args.get(name):
        raise ValueError(f"Verb {cls.name!r} requires argument {name!r}")

  @staticmethod
  def _bool_arg(kwargs: dict[str, str], name: str) -> bool:
    """The one truthiness convention for string args: 1, true, or yes."""
    return kwargs.get(name, "") in ("1", "true", "yes")
