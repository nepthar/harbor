from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harbor.lib.logtab import LogTab
from harbor.lib.store import JsonLogtabStore

# `pytester` runs a throwaway pytest inside a test, which is how
# tests/test_docker.py proves the docker guard actually fails a stray call.
pytest_plugins = ["pytester"]

FIXTURES = Path(__file__).parent / "fixtures" / "apps"

CONFIG = """\
apps_root = "apps"
run_root = "run"
volume_root = "volumes"
master_keyfile = "master.key"
domain = "harbor.localhost"
port_base = 41000
"""

FAKE_DOCKER = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_DOCKER_STATE"])
log = Path(os.environ["FAKE_DOCKER_LOG"])
with log.open("a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\\n")

containers = json.loads(state.read_text()) if state.exists() else []
if args[:3] == ["compose", "up", "-d"]:
    app_id = Path.cwd().name
    containers = [c for c in containers if c["app_id"] != app_id]
    containers.append({
        "app_id": app_id,
        "run_unit": "main",
        "id": "fake-container",
        "state": "running",
    })
    state.write_text(json.dumps(containers))
elif args[:2] == ["compose", "down"]:
    app_id = Path.cwd().name
    containers = [c for c in containers if c["app_id"] != app_id]
    if containers:
        state.write_text(json.dumps(containers))
    else:
        state.unlink(missing_ok=True)
elif args[:2] == ["ps", "-a"]:
    for container in containers:
        print(json.dumps({
            "ID": container["id"],
            "Names": f"{container['app_id']}-{container['run_unit']}-1",
            "State": container["state"],
            "Labels": (
                f"harbor.app_id={container['app_id']},"
                f"harbor.run_unit={container['run_unit']}"
            ),
        }))
"""


@dataclass(frozen=True)
class HarborEnv:
  root: Path
  config: Path
  docker_state: Path
  docker_log: Path

  @property
  def run_root(self) -> Path:
    return self.root / "run"

  @property
  def volumes_root(self) -> Path:
    return self.root / "volumes"

  @property
  def db_path(self) -> Path:
    return self.root / "harbordb.logtab"

  @property
  def harbor_lockfile_path(self) -> Path:
    return self.root / "harbor.lock"

  def read_db(self) -> dict[str, Any]:
    """Reconstruct the harbor DB as a nested dict from its flat logtab keys.

    The store persists flat ``section/.../key -> value`` entries (see
    :class:`harbor.lib.store.JsonConfigStore`). This rebuilds the nested shape
    tests assert against, e.g. ``db["routes"][app_id][route]`` and
    ``db["system"]["secrets"][name]``.
    """
    db: dict[str, Any] = {}
    for key, value in JsonLogtabStore(self.db_path).scan("").items():
      parts = key.split("/")
      if parts[0] == "apps":
        app_id, section, rest = parts[1], parts[2], "/".join(parts[3:])
        app = db.setdefault("apps", {}).setdefault(app_id, {})
        # metadata is flattened onto the app; other sections (config, binds)
        # keep their sub-mapping.
        if section == "metadata":
          app[rest] = value
        else:
          app.setdefault(section, {})[rest] = value
      else:
        # routes/<app>/<name>, system/secrets/<name>, …
        section, rest = parts[1], "/".join(parts[2:])
        db.setdefault(parts[0], {}).setdefault(section, {})[rest] = value
    return db

  def seed_db(self, entries: dict[str, Any]) -> None:
    """Write raw flat ``key -> value`` entries directly into the DB logtab."""
    store = JsonLogtabStore(self.db_path)
    for key, value in entries.items():
      store.write(key, value)

  def run(
    self,
    *args: str,
    input: str | None = None,
    timeout: float | None = None,
  ) -> subprocess.CompletedProcess[str]:
    """Run a harbor command.

    `timeout` raises `subprocess.TimeoutExpired` (killing the child) rather
    than hanging, which is how a test asserts that a command blocked -- harbor
    waits on `harbor.lock` indefinitely.
    """
    env = {
      **os.environ,
      "HARBOR_CONFIG": str(self.config),
      "FAKE_DOCKER_STATE": str(self.docker_state),
      "FAKE_DOCKER_LOG": str(self.docker_log),
      "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
      [sys.executable, "-m", "harbor.cli", *args],
      cwd=self.root,
      env=env,
      capture_output=True,
      text=True,
      input=input,
      timeout=timeout,
    )

  def set_containers(self, containers: list[dict[str, str]]) -> None:
    self.docker_state.write_text(json.dumps(containers))


# Records the attempt before refusing it. Exiting non-zero is not enough on its
# own: harbor calls docker with check=False in places (see
# `load_harbor_run_unit_status`), which turns a refusal into an empty result
# that looks exactly like "no containers are running". The log is what makes an
# accidental call visible no matter how the caller handles the failure.
DOCKER_GUARD = """#!/usr/bin/env python3
import os
import sys

invocation = "docker " + " ".join(sys.argv[1:])
log = os.environ.get("DOCKER_GUARD_LOG")
if log:
    with open(log, "a") as f:
        f.write(invocation + "\\n")

sys.exit(
    "tests must not shell out to the real docker daemon, but one invoked: "
    + invocation
)
"""


@pytest.fixture(autouse=True)
def block_real_docker(
  request: pytest.FixtureRequest,
  tmp_path_factory: pytest.TempPathFactory,
  monkeypatch: pytest.MonkeyPatch,
):
  """Shadow the real `docker` binary for every test that has not opted in.

  Autouse so this holds even for tests that never touch `harbor_env`. Two
  things happen: the call is refused, so a developer's own containers are never
  read or disturbed, and it is recorded, so the test fails even when harbor
  swallows the error. Tests marked `docker` want the real thing and are left
  alone.

  Yields the invocation log. A test that means to trip the guard should depend
  on `expect_docker_calls` instead of using this directly.
  """
  if "docker" in request.keywords:
    yield None
    return

  guard_dir = tmp_path_factory.mktemp("docker-guard")
  guard = guard_dir / "docker"
  guard.write_text(DOCKER_GUARD)
  guard.chmod(0o755)
  log = guard_dir / "invocations.log"

  monkeypatch.setenv("DOCKER_GUARD_LOG", str(log))
  monkeypatch.setenv("PATH", f"{guard_dir}{os.pathsep}{os.environ['PATH']}")

  yield log

  if log.exists():
    pytest.fail(
      "this test reached for the real docker binary:\n  "
      + "  ".join(log.read_text().splitlines(keepends=True))
      + "\nUse the harbor_env fixture, whose fake docker is safe to call."
    )


@pytest.fixture
def expect_docker_calls(block_real_docker: Path):
  """For tests that deliberately trip the guard, so it does not fail them."""
  yield block_real_docker
  block_real_docker.unlink(missing_ok=True)


@pytest.fixture
def harbor_env(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  block_real_docker: Path | None,
) -> HarborEnv:
  root = tmp_path / "harbor"
  apps = root / "apps"
  apps.mkdir(parents=True)
  (root / "run").mkdir()
  (root / "volumes").mkdir()

  for happ in FIXTURES.glob("*.happ"):
    shutil.copytree(happ, apps / happ.name)

  LogTab(root / "master.key").write("master_key", "0" * 64)
  config = root / "config.toml"
  config.write_text(CONFIG)

  bin_dir = root / "bin"
  bin_dir.mkdir()
  docker = bin_dir / "docker"
  docker.write_text(FAKE_DOCKER)
  docker.chmod(0o755)

  env = HarborEnv(
    root=root,
    config=config,
    docker_state=root / "docker-state",
    docker_log=root / "docker.log",
  )

  # Tests that drive lifecycle/HarborCtx in-process (rather than through
  # HarborEnv.run) need the working fake, not just the guard `block_real_docker`
  # installed. Prepending here puts it ahead of that guard on PATH.
  monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
  monkeypatch.setenv("FAKE_DOCKER_STATE", str(env.docker_state))
  monkeypatch.setenv("FAKE_DOCKER_LOG", str(env.docker_log))
  return env
