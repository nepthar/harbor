"""Harbor takes one lock per command invocation.

`harbor/cli/main.py` wraps the whole command in `ctx.lock()`. Two commands opt
out: `init`, which runs before there is a config to load, and `logs`, which
streams until interrupted.

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
from harbor.lib.harbor import LOCK_KEY, HarborCtx
from harbor.lib.logtab import LogTab
from tests.conftest import LOCK_TIMEOUT


def test_the_lock_is_recorded_then_released(harbor_env):
  """A held lock and a released one are both visible in the activity log."""
  ctx = HarborCtx(load_config_file(harbor_env.config))
  activity = LogTab(ctx.config.activity_log)

  with ctx.lock("ps"):
    held = json.loads(activity.read(LOCK_KEY))
    assert held["state"] == "acquired"
    assert held["by"] == "ps"
    assert held["pid"] == os.getpid()

  released = json.loads(activity.read(LOCK_KEY))
  assert released["state"] == "released"
  assert released["by"] == "ps"


def test_the_lock_timeout_message_names_the_holder(harbor_env):
  """The whole point of the record: explain a wait instead of just failing."""
  config = load_config_file(harbor_env.config)
  LogTab(config.activity_log).write(
    LOCK_KEY,
    json.dumps(
      {
        "state": "acquired",
        "by": "start ports-demo",
        "pid": 999999,
        "at": "2026-07-29T18:22:04-06:00",
      }
    ),
  )

  with FileLock(harbor_env.harbor_lockfile_path):
    blocked = harbor_env.run("ps")

  assert blocked.returncode == 1
  assert "start ports-demo" in blocked.stderr
  assert "999999" in blocked.stderr


def test_the_recorded_holder_is_the_command_not_the_argv(harbor_env):
  """`config --set k=secret` must never put the value in the activity log."""
  app_id = "io.p2net.basic-features"
  assert harbor_env.run("stage", app_id).returncode == 0
  assert harbor_env.run("config", app_id, "--set", "admin_pass=hunter2").returncode == 0

  config = load_config_file(harbor_env.config)
  recorded = LogTab(config.activity_log).read(LOCK_KEY)

  assert "hunter2" not in recorded
  assert json.loads(recorded)["by"] == f"config {app_id}"


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
