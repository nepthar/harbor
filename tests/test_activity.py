"""The activity log: harbor's own run output, on disk and indexed."""

from __future__ import annotations

import io
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest

from harbor.lib import activity
from harbor.lib.apps import AppID
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx


@pytest.fixture
def ctx(harbor_env):
  cfg = load_config()
  assert cfg is not None
  return HarborCtx(cfg)


def _record(ctx, verb="start", app="demo.app", status=activity.OK, output="hello"):
  started = datetime(2026, 8, 25, 3, 30, 0, tzinfo=UTC)
  return activity.record_run(
    ctx,
    verb,
    {"app": app or ""},
    app_id=AppID(app) if app else None,
    status=status,
    started=started,
    finished=started + timedelta(seconds=2, milliseconds=500),
    output=output,
  )


def test_a_run_leaves_a_file_and_an_index_row(ctx):
  relpath = _record(ctx, output="line one\nline two")

  path = ctx.config.activity_root / relpath
  assert relpath == "2026-08-25T033000Z.demo.app.start.log"
  assert path == ctx.config.harbor_root / "var" / "logs" / relpath
  assert path.is_file()
  body = path.read_text()
  assert "line one\nline two" in body
  assert "# harbor start demo.app" in body
  assert "— ok" in body

  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["app_id"] == "demo.app"
  assert runs[0]["verb"] == "start"
  assert runs[0]["status"] == "ok"
  assert runs[0]["duration_ms"] == 2500
  assert runs[0]["available"] is True
  assert runs[0]["log"] == relpath


def test_runs_list_newest_first_and_filter_by_app(ctx):
  _record(ctx, app="one")
  _record(ctx, app="two")
  _record(ctx, app="one")

  everything = activity.list_runs(ctx)
  assert [r["app_id"] for r in everything] == ["one", "two", "one"]

  just_one = activity.list_runs(ctx, app="one")
  assert len(just_one) == 2
  assert all(r["app_id"] == "one" for r in just_one)


def test_appless_runs_omit_the_app_id(ctx):
  relpath = _record(ctx, verb="fetch", app=None, output="Installed x")
  assert relpath == "2026-08-25T033000Z.fetch.log"

  runs = activity.list_runs(ctx, app=activity.HARBOR_DIR)
  assert len(runs) == 1
  assert runs[0]["app_id"] is None
  assert runs[0]["verb"] == "fetch"


def test_reading_a_run_log_by_name(ctx):
  relpath = _record(ctx, output="the output")
  text = activity.read_run_log(ctx, relpath)
  assert "the output" in text


def test_reading_refuses_traversal_and_unknown_files(ctx):
  _record(ctx)
  with pytest.raises(ValueError):
    activity.read_run_log(ctx, "../../../etc/passwd")
  with pytest.raises(ValueError):
    activity.read_run_log(ctx, "nope.log")
  with pytest.raises(ValueError):
    activity.read_run_log(ctx, "..")


def test_pruning_keeps_the_newest_files_but_the_index_remembers(ctx, monkeypatch):
  monkeypatch.setattr(activity, "KEEP_RUNS", 3)
  started = datetime(2026, 8, 25, 3, 0, 0, tzinfo=UTC)
  for i in range(5):
    activity.record_run(
      ctx,
      "start",
      {"app": "demo.app"},
      app_id=AppID("demo.app"),
      status=activity.OK,
      started=started + timedelta(minutes=i),
      finished=started + timedelta(minutes=i, seconds=1),
      output=f"run {i}",
    )

  files = sorted(ctx.config.activity_root.glob("*.log"))
  assert len(files) == 3

  runs = activity.list_runs(ctx, limit=10)
  assert len(runs) == 5
  # The two oldest lost their files but not their index rows.
  assert [r["available"] for r in runs] == [True, True, True, False, False]


def test_two_runs_in_one_second_do_not_collide(ctx):
  a = _record(ctx, output="first")
  b = _record(ctx, output="second")
  assert a == "2026-08-25T033000Z.demo.app.start.log"
  assert b == "2026-08-25T033000Z.demo.app.start-2.log"
  assert (ctx.config.activity_root / a).read_text() != (
    ctx.config.activity_root / b
  ).read_text()


def test_begin_run_is_readable_before_finish(ctx):
  started = datetime(2026, 8, 25, 3, 30, tzinfo=UTC)
  relpath = activity.begin_run(
    ctx,
    "cmd",
    {"app": "demo.app", "command": "ping"},
    app_id=AppID("demo.app"),
    started=started,
  )
  path = ctx.config.activity_root / relpath
  assert "# harbor cmd demo.app" in path.read_text()
  assert activity.list_runs(ctx) == []

  with path.open("a") as log:
    log.write("pong\n")
  assert "pong" in activity.read_run_log(ctx, path.name)

  finished = started + timedelta(seconds=1)
  activity.finish_run(
    ctx,
    relpath,
    "cmd",
    app_id=AppID("demo.app"),
    status=activity.OK,
    started=started,
    finished=finished,
  )
  body = path.read_text()
  assert "— ok" in body
  assert "pong" in body
  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["log"] == relpath
  assert runs[0]["status"] == "ok"


def test_activity_records_a_free_form_block(ctx):
  """The context manager, used the way a CLI verb would use it."""
  with activity.Activity(ctx, "refresh", app="demo.app") as act:
    logging.getLogger("harbor.lifecycle").info("stopped")
    act.write("copied 3 volumes\n")
    logging.getLogger("harbor.lifecycle").info("started")

  assert act.error is None
  body = (ctx.config.activity_root / act.log).read_text()
  assert "# harbor refresh demo.app" in body
  assert re.search(r"\d\d:\d\d:\d\dZ \w+ +stopped", body)
  assert "copied 3 volumes" in body
  assert re.search(r"\d\d:\d\d:\d\dZ \w+ +started", body)
  assert "— ok" in body

  runs = activity.list_runs(ctx)
  assert len(runs) == 1
  assert runs[0]["verb"] == "refresh"
  assert runs[0]["app_id"] == "demo.app"
  assert runs[0]["status"] == "ok"
  assert runs[0]["log"] == act.log


def test_activity_records_a_failure_and_reraises(ctx):
  act = activity.Activity(ctx, "refresh", app="demo.app")
  with pytest.raises(ValueError, match="volume is gone"), act:
    logging.getLogger("harbor.lifecycle").info("stopping")
    raise ValueError("volume is gone")

  assert act.error == "volume is gone"
  body = (ctx.config.activity_root / act.log).read_text()
  assert "stopping" in body
  assert "Error: volume is gone" in body
  assert "— error" in body
  assert activity.list_runs(ctx)[0]["status"] == "error"


def test_activity_echo_copies_the_log_to_a_stream(ctx):
  echo = io.StringIO()
  with activity.Activity(ctx, "refresh", echo=echo) as act:
    logging.getLogger("harbor").info("working")

  # Same bytes, both places -- but the trailer belongs to the file alone.
  assert "working" in echo.getvalue()
  assert echo.getvalue() in (ctx.config.activity_root / act.log).read_text()
  # No app: the filename carries the verb and nothing else.
  assert act.log.endswith(".refresh.log")


def test_activity_subprocess_streams_into_the_log(ctx):
  with activity.Activity(ctx, "probe") as act:
    act.subprocess(["echo", "from the child"])
    data = act.subprocess(["echo", '{"a": 1}'], parse_json=True)
  assert data == {"a": 1}

  body = (ctx.config.activity_root / act.log).read_text()
  assert "from the child" in body
  # Parsed JSON is a return value, not output.
  assert '{"a": 1}' not in body


def test_activity_outside_its_block_is_refused(ctx):
  act = activity.Activity(ctx, "probe")
  with pytest.raises(RuntimeError, match="not running"):
    act.write("nope")
  with pytest.raises(RuntimeError, match="not running"):
    act.subprocess(["true"])
  assert activity.list_runs(ctx) == []
