"""The activity log: harbor's own run output, on disk and indexed."""

from __future__ import annotations

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
  assert path == ctx.config.harbor_root / "var" / "logs" / relpath
  assert path.is_file()
  body = path.read_text()
  assert "line one\nline two" in body
  assert "# harbor start demo.app — ok" in body

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


def test_appless_runs_file_under_harbor(ctx):
  relpath = _record(ctx, verb="fetch", app=None, output="Installed x")
  assert relpath.startswith(f"{activity.HARBOR_DIR}/")

  runs = activity.list_runs(ctx, app=activity.HARBOR_DIR)
  assert len(runs) == 1
  assert runs[0]["app_id"] is None
  assert runs[0]["verb"] == "fetch"


def test_reading_a_run_log_by_name(ctx):
  relpath = _record(ctx, output="the output")
  dirname, _, filename = relpath.partition("/")
  text = activity.read_run_log(ctx, dirname, filename)
  assert "the output" in text


def test_reading_refuses_traversal_and_unknown_files(ctx):
  _record(ctx)
  with pytest.raises(ValueError):
    activity.read_run_log(ctx, "demo.app", "../../../etc/passwd")
  with pytest.raises(ValueError):
    activity.read_run_log(ctx, "demo.app", "nope.log")
  with pytest.raises(ValueError):
    activity.read_run_log(ctx, "..", "anything.log")


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

  files = sorted((ctx.config.activity_root / "demo.app").glob("*.log"))
  assert len(files) == 3

  runs = activity.list_runs(ctx, limit=10)
  assert len(runs) == 5
  # The two oldest lost their files but not their index rows.
  assert [r["available"] for r in runs] == [True, True, True, False, False]


def test_two_runs_in_one_second_do_not_collide(ctx):
  a = _record(ctx, output="first")
  b = _record(ctx, output="second")
  assert a != b
  assert (ctx.config.activity_root / a).read_text() != (
    ctx.config.activity_root / b
  ).read_text()
