"""A harbor verb the daemon can be asked for over the wire.

A Job is the adapter between a request -- a verb name and a flat dict of
string arguments -- and the work itself. ``init`` parses those strings
against a live context and refuses anything it cannot use. ``run`` does the
work, by calling the same ``harbor.lib`` functions the CLI calls. Everything
else here is the wire: an id, a state a poller can read, and the argument
checking that lets the API reject a bad request before anything happens.

The recording is not this module's job. ``execute`` wraps ``run`` in an
``Activity``, which owns the log file, the output capture and the index row;
see ``harbor.lib.activity``. Code that wants a run recorded but has no wire
to answer to -- the CLI -- uses ``Activity`` directly and skips all of this.

A job that needs another verb's work calls the lifecycle functions directly,
the way ``SnapshotJob`` calls ``stop`` and ``start``. Never submit to the
runner from inside ``run``: the queue is serial, so a parent waiting on its
own child waits forever.
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
  # Resolved app id, when the job is about one. Set in ``init`` so the log
  # files under that app; unknown ids still file under ``harbor/``.
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
    """Run the work as one recorded Activity, then re-raise anything it hit.

    ``echo`` is passed straight through: a caller with a terminal sees the
    same bytes the log file gets. The daemon leaves it unset -- the UI polls
    ``self.log`` instead.
    """
    self.state = RUNNING
    self.started_at = _now()
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

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    """Parse arguments that already passed ``required_args`` / ``optional_args``.

    Raise ``ValueError`` if they are not usable: an app that does not
    resolve, a snapshot that is not there, a config value that cannot be
    read. ``execute`` will not run, and no log is written, if this raises.
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
