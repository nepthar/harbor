"""CLI commands take a harbor lock, and app commands take an app lock first.

`logs` / `activity` / `decrypt` take neither: they stream or read.
`cmd` holds the app lock while the command runs, not the harbor lock.
`init` runs before there is a config to lock.

Most of these run in-process -- `filelock` blocks a second acquire from the
same process just as it does across processes. The one test that would be
vacuous that way spawns a real child.
"""

from __future__ import annotations

import json
import os
import time

from filelock import FileLock

from harbor.lib.config import load_config_file
from harbor.lib.harbor import HarborCtx
from tests.conftest import LOCK_TIMEOUT


def _holder(path) -> dict:
  return json.loads(path.read_text())


def test_the_lock_is_recorded_in_the_lockfile(harbor_env):
  """A held lock overwrites the lockfile; release leaves the last holder."""
  ctx = HarborCtx(load_config_file(harbor_env.config))
  assert (
    ctx.config.harbor_lockfile_path == harbor_env.root / "var" / "lock" / "harbor.lock"
  )

  with ctx.harbor_lock("ps"):
    held = _holder(ctx.config.harbor_lockfile_path)
    assert held["by"] == "ps"
    assert held["pid"] == os.getpid()
    assert "at" in held
    assert "state" not in held

  released = _holder(ctx.config.harbor_lockfile_path)
  assert released["by"] == "ps"
  assert released["pid"] == os.getpid()


def test_the_lock_timeout_message_names_the_holder(harbor_env):
  """The whole point of the record: explain a wait instead of just failing."""
  with FileLock(harbor_env.harbor_lockfile_path):
    harbor_env.harbor_lockfile_path.write_text(
      json.dumps(
        {"by": "start ports-demo", "pid": 999999, "at": "2026-01-01T00:00:00Z"}
      )
    )
    blocked = harbor_env.run("ps")

  assert blocked.returncode == 1
  assert "start ports-demo" in blocked.stderr
  assert "999999" in blocked.stderr


def test_the_recorded_holder_is_the_command_not_the_argv(harbor_env):
  """`config --set k=secret` must never put the value in the lockfile."""
  app_id = "io.p2net.basic-features"
  assert harbor_env.run("stage", app_id).returncode == 0
  assert harbor_env.run("config", app_id, "--set", "admin_pass=hunter2").returncode == 0

  recorded = harbor_env.harbor_lockfile_path.read_text()
  assert "hunter2" not in recorded
  assert _holder(harbor_env.harbor_lockfile_path)["by"] == f"config {app_id}"


def test_a_held_lock_makes_a_command_give_up_with_a_reason(harbor_env):
  """Harbor must wait rather than run concurrently -- and then give up, rather
  than hang forever with nothing on screen to explain why."""
  lock = FileLock(harbor_env.harbor_lockfile_path)

  # Free when nothing is running.
  lock.acquire(timeout=0)
  lock.release()

  with lock:
    started = time.monotonic()
    result = harbor_env.run("ps")
    waited = time.monotonic() - started

  assert result.returncode == 1
  assert "Another process has locked harbor" in result.stderr
  assert f"{LOCK_TIMEOUT:g} seconds" in result.stderr
  # The wait must actually end on its own, not just eventually.
  assert LOCK_TIMEOUT <= waited < LOCK_TIMEOUT + 5, waited

  # ...and it proceeds again once released.
  assert harbor_env.run("ps").returncode == 0


def test_a_second_harbor_process_waits_on_the_first(harbor_env):
  """The cross-process claim the in-process tests stand in for.

  Everything else here would still pass if the lock were a plain in-memory
  flag, so this one pays for a real interpreter.
  """
  with FileLock(harbor_env.harbor_lockfile_path):
    result = harbor_env.run_subprocess("ps", timeout=30)

  assert result.returncode == 1
  assert "Another process has locked harbor" in result.stderr


def test_cmd_does_not_hold_the_harbor_lock(harbor_env):
  """A long-running command must not lock other apps out of harbor."""
  _stage_cmd_demo(harbor_env)

  with FileLock(harbor_env.harbor_lockfile_path):
    ran = harbor_env.run("cmd", "cmd-demo", "ping")
    blocked = harbor_env.run("ps")

  assert ran.returncode == 0, ran.stderr
  assert "locked harbor" not in ran.stderr
  assert blocked.returncode == 1
  assert "Another process has locked harbor" in blocked.stderr


def test_cmd_holds_the_app_lock(harbor_env):
  """The same app cannot be started while a command is running; others can."""
  _stage_cmd_demo(harbor_env)
  assert harbor_env.run("stage", "routes-demo").returncode == 0
  lock = FileLock(harbor_env.app_lockfile_path("cmd-demo"))
  with lock:
    blocked = harbor_env.run("cmd", "cmd-demo", "ping")
    other = harbor_env.run("start", "routes-demo")

  assert blocked.returncode == 1
  assert "locked app cmd-demo" in blocked.stderr
  assert other.returncode == 0, other.stderr


def _stage_cmd_demo(harbor_env):
  app = harbor_env.root / "apps" / "cmd-demo.happ"
  app.mkdir()
  (app / "manifest.toml").write_text(
    """\
[app]
version = "1"

[run.main]
image = "alpine:latest"
cmd = ["/bin/sh", "-c", "sleep infinity"]

[commands.ping]
cmd = "echo pong"
"""
  )
  assert harbor_env.run("stage", "cmd-demo").returncode == 0


def test_logs_does_not_hold_the_harbor_lock(harbor_env):
  """`logs -f` streams until interrupted.

  Holding the lock for that long would lock the operator out of harbor for as
  long as they watch logs, so this command runs unlocked.
  """
  assert harbor_env.run("start", "ports-demo").returncode == 0

  with FileLock(harbor_env.harbor_lockfile_path):
    tailed = harbor_env.run("logs", "ports-demo")
    # ...while a state-changing command still waits and gives up.
    blocked = harbor_env.run("ps")

  assert tailed.returncode == 0, tailed.stderr
  assert "locked harbor" not in tailed.stderr
  assert blocked.returncode == 1
  assert "Another process has locked harbor" in blocked.stderr


def test_an_app_lock_does_not_block_another_app(harbor_env):
  """The point of per-app locks: snapshotting A must not stall start B."""
  assert harbor_env.run("stage", "ports-demo").returncode == 0
  lock = FileLock(harbor_env.app_lockfile_path("ports-demo"))
  with lock:
    started = harbor_env.run("start", "routes-demo")
    blocked = harbor_env.run("start", "ports-demo")

  assert started.returncode == 0, started.stderr
  assert blocked.returncode == 1
  assert "locked app ports-demo" in blocked.stderr


def test_ps_does_not_need_the_app_lock(harbor_env):
  """A harbor-wide read proceeds while one app is locked for a long copy."""
  with FileLock(harbor_env.app_lockfile_path("ports-demo")):
    listed = harbor_env.run("ps")

  assert listed.returncode == 0, listed.stderr
  assert "locked" not in listed.stderr


def test_snapshot_releases_harbor_while_copying(harbor_env, monkeypatch):
  """Other apps can take the harbor lock during the volume copy."""
  import importlib

  snapshot_mod = importlib.import_module("harbor.lib.lifecycle.snapshot")

  assert harbor_env.run("stage", "ports-demo").returncode == 0
  original = snapshot_mod.snapshot

  def during_copy(app, ctx, label=""):
    lock = FileLock(ctx.config.harbor_lockfile_path)
    lock.acquire(timeout=0)
    lock.release()
    return original(app, ctx, label=label)

  monkeypatch.setattr(snapshot_mod, "snapshot", during_copy)
  taken = harbor_env.run("snapshot", "ports-demo", "--label", "copy")
  assert taken.returncode == 0, taken.stderr
