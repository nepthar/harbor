"""Operation: parse first, then file a run log, then do the work."""

from __future__ import annotations

import pytest

from harbor.lib import activity
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.ops import DONE, SleepOp
from harbor.ops.operation import BaseOp


@pytest.fixture
def ctx(harbor_env) -> HarborCtx:
  cfg = load_config()
  assert cfg is not None
  return HarborCtx(cfg)


def test_call_files_an_activity_log(ctx):
  op = SleepOp.call({"seconds": "0", "say_name": "harbor"}, ctx)

  assert op.state == DONE
  assert op.error is None
  assert op.id
  assert "Hello, harbor! Sleeping for 0 seconds..." in op.output
  assert "Done sleeping for 0 seconds!" in op.output

  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["verb"] == "sleep"
  assert runs[0]["app_id"] is None
  assert runs[0]["status"] == "ok"
  assert runs[0]["log"].startswith(f"{activity.HARBOR_DIR}/")
  assert op.log == runs[0]["log"]

  body = (ctx.config.activity_root / runs[0]["log"]).read_text()
  assert "# harbor sleep — ok" in body
  assert "Hello, harbor!" in body
  assert "Done sleeping" in body


def test_init_failure_does_not_file_a_log(ctx):
  with pytest.raises(ValueError, match="seconds"):
    SleepOp.call({"seconds": "nope"}, ctx)
  assert activity.list_runs(ctx) == []


def test_unknown_argument_is_refused_before_run(ctx):
  with pytest.raises(ValueError, match="takes no argument 'bogus'"):
    SleepOp.call({"seconds": "0", "bogus": "1"}, ctx)
  assert activity.list_runs(ctx) == []


def test_missing_seconds_is_refused(ctx):
  with pytest.raises(ValueError, match="requires argument 'seconds'"):
    SleepOp.call({}, ctx)


def test_failed_subprocess_files_an_error_log(ctx):
  class FailOp(SleepOp):
    def run(self, ctx) -> None:
      self.subprocess(["false"])

  with pytest.raises(RuntimeError, match="exited with status"):
    FailOp.call({"seconds": "0"}, ctx)

  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["status"] == "error"
  body = (ctx.config.activity_root / runs[0]["log"]).read_text()
  assert "— error" in body
  assert "exited with status" in body


def test_json_subprocess_is_captured_not_streamed(ctx):
  class JsonOp(BaseOp):
    name = "json-probe"

    def init(self, ctx, kwargs: dict[str, str]) -> None:
      pass

    def run(self, ctx) -> None:
      data = self.subprocess(["echo", '{"a": 1}'], json=True)
      assert data == {"a": 1}

  op = JsonOp.call({}, ctx)
  assert op.state == DONE
  assert '{"a": 1}' not in op.output


def test_as_dict_looks_like_a_job(ctx):
  op = SleepOp.call({"seconds": "0"}, ctx)
  payload = op.as_dict()
  assert payload["verb"] == "sleep"
  assert payload["state"] == DONE
  assert payload["args"] == {"seconds": "0"}
  assert payload["id"] == op.id
  assert payload["log"] == op.log
  assert payload["error"] is None
  assert payload["started_at"] and payload["finished_at"]
