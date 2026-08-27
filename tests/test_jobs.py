"""Job: parse first, then file a run log, then do the work."""

from __future__ import annotations

import io

import pytest

from harbor.jobs import DONE, Job
from harbor.jobs.job import logger
from harbor.lib import activity
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx


class SleepJob(Job):
  name = "sleep"
  description = "Sleep for a given number of seconds"
  required_args = ("seconds",)
  optional_args = ("say_name",)

  def init(self, ctx, kwargs: dict[str, str]) -> None:
    raw = kwargs["seconds"]
    try:
      self.seconds = int(raw)
    except ValueError:
      raise ValueError(f"sleep seconds must be an integer, not {raw!r}") from None
    self.say_name = kwargs.get("say_name", "stranger")

  def run(self, ctx) -> None:
    logger.info("Hello, %s! Sleeping for %s seconds...", self.say_name, self.seconds)
    self.subprocess(["sleep", str(self.seconds)])
    logger.info("Done sleeping for %s seconds!", self.seconds)


@pytest.fixture
def ctx(harbor_env) -> HarborCtx:
  cfg = load_config()
  assert cfg is not None
  return HarborCtx(cfg)


def _log_text(ctx, job) -> str:
  assert job.log
  return (ctx.config.activity_root / job.log).read_text()


def test_call_files_an_activity_log(ctx):
  job = SleepJob.call({"seconds": "0", "say_name": "harbor"}, ctx)

  assert job.state == DONE
  assert job.error is None
  assert job.id

  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["verb"] == "sleep"
  assert runs[0]["app_id"] is None
  assert runs[0]["status"] == "ok"
  assert runs[0]["log"].endswith(".sleep.log")
  assert "/" not in runs[0]["log"]
  assert job.log == runs[0]["log"]

  body = _log_text(ctx, job)
  assert "# harbor sleep" in body
  assert "Hello, harbor! Sleeping for 0 seconds..." in body
  assert "Done sleeping for 0 seconds!" in body
  assert "— ok" in body


def test_echo_copies_the_log_to_the_stream(ctx):
  echo = io.StringIO()
  job = SleepJob.call({"seconds": "0", "say_name": "harbor"}, ctx, echo=echo)

  assert job.state == DONE
  # Same bytes, both places: the terminal sees what the log file keeps.
  assert "Hello, harbor!" in echo.getvalue()
  assert echo.getvalue() in _log_text(ctx, job)


def test_init_failure_does_not_file_a_log(ctx):
  with pytest.raises(ValueError, match="seconds"):
    SleepJob.call({"seconds": "nope"}, ctx)
  assert activity.list_runs(ctx) == []


def test_unknown_argument_is_refused_before_run(ctx):
  with pytest.raises(ValueError, match="takes no argument 'bogus'"):
    SleepJob.call({"seconds": "0", "bogus": "1"}, ctx)
  assert activity.list_runs(ctx) == []


def test_missing_seconds_is_refused(ctx):
  with pytest.raises(ValueError, match="requires argument 'seconds'"):
    SleepJob.call({}, ctx)


def test_failed_subprocess_files_an_error_log(ctx):
  class FailJob(SleepJob):
    def run(self, ctx) -> None:
      self.subprocess(["false"])

  with pytest.raises(RuntimeError, match="exited with status"):
    FailJob.call({"seconds": "0"}, ctx)

  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["status"] == "error"
  body = (ctx.config.activity_root / runs[0]["log"]).read_text()
  assert "— error" in body
  assert "exited with status" in body


def test_json_subprocess_is_captured_not_streamed(ctx):
  class JsonJob(Job):
    name = "json-probe"

    def init(self, ctx, kwargs: dict[str, str]) -> None:
      pass

    def run(self, ctx) -> None:
      data = self.subprocess(["echo", '{"a": 1}'], parse_json=True)
      assert data == {"a": 1}

  job = JsonJob.call({}, ctx)
  assert job.state == DONE
  assert '{"a": 1}' not in _log_text(ctx, job)


def test_as_dict_carries_no_output(ctx):
  job = SleepJob.call({"seconds": "0"}, ctx)
  payload = job.as_dict()
  assert payload["verb"] == "sleep"
  assert payload["state"] == DONE
  assert payload["args"] == {"seconds": "0"}
  assert payload["id"] == job.id
  assert payload["log"] == job.log
  assert payload["error"] is None
  assert payload["started_at"] and payload["finished_at"]
  assert "output" not in payload
