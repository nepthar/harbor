"""A business-logic task that CLI and daemon both invoke the same way.

An Operation is the job: it has an id, a state, and a log file under
``$harbor/var/logs``. ``init`` parses string arguments against a live context
and refuses anything it cannot use. ``run`` does the work. ``execute`` is what
files the log, redirects ``harbor.*`` logging and streamed subprocess output
into it, and records how the run ended.
"""

from __future__ import annotations

import io
import json as json_lib
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TextIO

from harbor.lib import activity
from harbor.lib.apps import AppID
from harbor.lib.docker import sink_output
from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.ops")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def _now() -> str:
  return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _capture_harbor_logging(stream: io.TextIOBase) -> Iterator[None]:
  """Tee ``harbor.*`` log records into ``stream`` for the duration.

  Progress that library code writes through ``logging`` -- container steps,
  route-provider chatter -- has to land in the run log, not only on this
  process's stderr. The logger is opened up to INFO while an op runs; callers
  run ops serially, so the widening never spans two ops.
  """
  handler = logging.StreamHandler(stream)
  handler.setLevel(logging.INFO)
  handler.setFormatter(logging.Formatter("%(message)s"))
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


class _Tee(io.TextIOBase):
  """Memory capture plus a flushed log file, so a poller can read mid-run.

  ``echo`` is the CLI case: the same bytes also go to the operator's terminal.
  """

  def __init__(
    self, memory: io.StringIO, log: io.TextIOBase, echo: TextIO | None = None
  ) -> None:
    self._memory = memory
    self._log = log
    self._echo = echo

  def write(self, s: str) -> int:
    self._memory.write(s)
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


class BaseOp:
  name: str
  description: str
  required_args: tuple[str, ...] = ()
  optional_args: tuple[str, ...] = ()
  # Resolved app id, when the op is about one. Set in ``init`` so the log
  # files under that app; unknown ids still file under ``harbor/``.
  app: str | None = None

  def __init__(self) -> None:
    self.id: str = uuid.uuid4().hex[:12]
    self.args: dict[str, str] = {}
    self.state: str = QUEUED
    self.output: str = ""
    self.error: str | None = None
    self.created_at: str = _now()
    self.started_at: str | None = None
    self.finished_at: str | None = None
    self.log: str | None = None
    self._sink: TextIO | None = None

  def as_dict(self) -> dict[str, Any]:
    return {
      "id": self.id,
      "verb": self.name,
      "args": dict(self.args),
      "state": self.state,
      "output": self.output,
      "error": self.error,
      "created_at": self.created_at,
      "started_at": self.started_at,
      "finished_at": self.finished_at,
      "log": self.log,
    }

  @classmethod
  def call(cls, args: dict[str, str], ctx: HarborCtx, *, echo: bool = False) -> BaseOp:
    """Parse, execute, re-raise on failure. Returns the finished op on success."""
    op = cls()
    op.args = dict(args)
    cls._check_args(args)
    op.init(ctx, args)
    op.execute(ctx, echo=echo)
    return op

  def execute(self, ctx: HarborCtx, *, echo: bool = False) -> None:
    """File the run log, redirect output, ``run``. Re-raises after recording.

    ``echo=True`` also writes the stream to stdout -- the CLI watching a
    terminal. The daemon leaves it off: the UI polls ``self.log`` instead.
    """
    self.state = RUNNING
    self.started_at = _now()
    started = datetime.now(UTC)
    captured = io.StringIO()
    log_file = None
    ok = False
    error: str | None = None
    try:
      app_id = self._app_id()
      relpath = activity.begin_run(
        ctx, self.name, self.args, app_id=app_id, started=started
      )
      self.log = relpath
      log_file = open(  # noqa: SIM115 - closed in the finally below
        ctx.config.activity_root / relpath, "a", encoding="utf-8", buffering=1
      )
      tee = _Tee(captured, log_file, echo=sys.stdout if echo else None)
      self._sink = tee
      with _capture_harbor_logging(tee), sink_output(tee):
        self.run(ctx)
      ok = True
    except (ValueError, RuntimeError) as e:
      error = str(e)
      raise
    except Exception as e:
      error = f"{type(e).__name__}: {e}"
      raise
    finally:
      self._sink = None
      if log_file is not None:
        log_file.close()
      finished = datetime.now(UTC)
      captured_text = captured.getvalue().rstrip()
      self.output = captured_text
      self.error = error
      self.state = DONE if ok else FAILED
      self.finished_at = _now()
      if self.log is not None:
        try:
          activity.finish_run(
            ctx,
            self.log,
            self.name,
            self.args,
            app_id=self._app_id(),
            status=activity.OK if ok else activity.ERROR,
            started=started,
            finished=finished,
            output="\n".join(p for p in (captured_text, error) if p),
          )
        except Exception:  # noqa: BLE001 - a log failure must not hide the op's own error
          logger.exception("could not record activity for `%s`", self.name)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    """Parse arguments that already passed ``required_args`` / ``optional_args``.

    Raise ``ValueError`` if they are not usable: an app that does not
    resolve, a snapshot that is not there, a config value that cannot be
    read. ``execute`` will not run, and no log is written, if this raises.
    """

  def run(self, ctx: HarborCtx) -> None:
    raise NotImplementedError

  def subprocess(
    self,
    cmd: list[str],
    *,
    json: bool = False,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
  ) -> Any:
    """Run ``cmd``. JSON out is captured and parsed; anything else is streamed.

    Streamed output goes to the run log (and stdout when ``execute(echo=True)``)
    as the child writes, so a poller reading the log sees progress. A
    non-zero exit is a ``RuntimeError`` naming the command.
    """
    if self._sink is None:
      raise RuntimeError(
        f"{self.name} subprocess is only available while the operation is running"
      )
    run_env = {**os.environ, **env} if env else None
    if json:
      result = subprocess.run(cmd, capture_output=True, text=True, env=run_env, cwd=cwd)
      if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
          f"Command {cmd[0]!r} exited with status {result.returncode}"
          + (f": {detail}" if detail else "")
        )
      try:
        return json_lib.loads(result.stdout)
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
        self._sink.flush()
    finally:
      stdout.close()
      code = proc.wait()
    if code != 0:
      raise RuntimeError(f"Command {cmd[0]!r} exited with status {code}")
    return None

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

  def _app_id(self) -> AppID | None:
    if not self.app:
      return None
    try:
      return AppID(self.app)
    except ValueError:
      return None


class SleepOp(BaseOp):
  name = "sleep"
  description = "Sleep for a given number of seconds"
  required_args = ("seconds",)
  optional_args = ("say_name",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    raw = kwargs["seconds"]
    try:
      self.seconds = int(raw)
    except ValueError:
      raise ValueError(f"sleep seconds must be an integer, not {raw!r}") from None
    self.say_name = kwargs.get("say_name", "stranger")

  def run(self, ctx: HarborCtx) -> None:
    logger.info("Hello, %s! Sleeping for %s seconds...", self.say_name, self.seconds)
    self.subprocess(["sleep", str(self.seconds)])
    logger.info("Done sleeping for %s seconds!", self.seconds)
