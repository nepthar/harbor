import os
import shutil
import socket
import subprocess
import sys

import pytest

from harbor.lib.logtab import LogTab


def _free_port() -> int:
  with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    return sock.getsockname()[1]


# --- the suite must never touch the real docker daemon ---------------------
#
# These guard the guard: `block_real_docker` in conftest.py shadows the real
# binary for every unmarked test. Without it, anything calling docker reads --
# or disturbs -- whatever the developer has running, which silently couples
# results to the machine (a real bug: test_curated_examples_materialize once
# failed because an example happ was genuinely running).


def test_docker_on_path_is_the_guard():
  found = shutil.which("docker")
  assert found is not None and "docker-guard" in found, found


def test_invoking_docker_fails_loudly(expect_docker_calls):
  result = subprocess.run(["docker", "ps", "-a"], capture_output=True, text=True)
  assert result.returncode != 0
  assert "must not shell out to the real docker" in result.stderr


def test_harbor_env_prefers_the_working_fake(harbor_env):
  assert shutil.which("docker") == str(harbor_env.root / "bin" / "docker")


def test_a_refused_call_is_recorded_even_though_harbor_swallows_it(
  expect_docker_calls,
):
  """The reason the guard logs instead of only exiting non-zero.

  `load_harbor_run_unit_status` passes check=False, so a refusal comes back as
  an empty mapping -- identical to a successful call on a machine with nothing
  running. Asserting on the return value therefore proves nothing. The log is
  what distinguishes "we were blocked" from "there was nothing to see".
  """
  from harbor.lib.docker import load_harbor_run_unit_status

  assert load_harbor_run_unit_status() == {}  # the swallowed failure
  assert "docker ps -a" in expect_docker_calls.read_text()  # the real evidence


@pytest.mark.docker
def test_real_docker_up_and_down(tmp_path):
  if shutil.which("docker") is None:
    pytest.skip("docker is not installed")
  if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("docker daemon is not available")

  root = tmp_path / "harbor"
  app = root / "apps" / "docker-smoke.happ"
  app.mkdir(parents=True)
  (root / "run").mkdir()
  (root / "volumes").mkdir()
  port = _free_port()
  (app / "manifest.toml").write_text(
    f"""\
[app]
version = "0.1.0"

[run.main]
image = "nginx:alpine"

[run.main.routes]
main = {{ port = "{port}:80" }}
"""
  )
  LogTab(root / "master.key").write("master_key", "0" * 64)
  config = root / "config.toml"
  config.write_text(
    """\
apps_root = "apps"
run_root = "run"
volume_root = "volumes"
master_keyfile = "master.key"
domain = "harbor.localhost"
port_base = 41000
"""
  )
  env = {**os.environ, "HARBOR_CONFIG": str(config)}

  def harbor(*args):
    return subprocess.run(
      [sys.executable, "-m", "harbor.cli", *args],
      env=env,
      capture_output=True,
      text=True,
    )

  try:
    started = harbor("up", "docker-smoke")
    assert started.returncode == 0, started.stderr

    containers = subprocess.run(
      [
        "docker",
        "ps",
        "-q",
        "--filter",
        "label=harbor.app_id=docker-smoke",
      ],
      capture_output=True,
      text=True,
      check=True,
    ).stdout.splitlines()
    assert len(containers) == 1
    published = subprocess.run(
      ["docker", "port", containers[0], "80/tcp"],
      capture_output=True,
      text=True,
      check=True,
    ).stdout
    assert str(port) in published
  finally:
    harbor("down", "docker-smoke")
