from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from filelock import FileLock

from harbor.lib import lifecycle
from harbor.lib.apps import read_app_actions, read_last_app_action
from harbor.lib.config import VOLUME_KINDS, load_config_file
from harbor.lib.harbor import LOCK_KEY, LOCK_TIMEOUT, HarborCtx
from harbor.lib.logtab import LogTab
from harbor.lib.store import HarborDB


def test_up_materializes_compose_and_port_state(harbor_env):
  result = harbor_env.run("up", "ports-demo")

  assert result.returncode == 0, result.stderr
  compose = yaml.safe_load(
    (harbor_env.run_root / "ports-demo" / "compose.yml").read_text()
  )
  assert compose["name"] == "ports-demo"
  assert compose["services"]["main"]["ports"] == ["41000:8080", "9000:80"]

  db = harbor_env.read_db()
  web = db["routes"]["ports-demo"]["web"]
  admin = db["routes"]["ports-demo"]["admin"]
  assert web["host_port"] == 41000
  assert web["publish"] == "lan"
  assert admin["host_port"] == 9000
  assert admin["publish"] == "lan"


def test_up_ps_down_tracks_docker_reality(harbor_env):
  catalog = harbor_env.run("catalog")
  assert catalog.returncode == 0, catalog.stderr
  assert "ports-demo" in catalog.stdout

  not_installed = harbor_env.run("ps")
  assert not_installed.returncode == 0
  assert "ports-demo" not in not_installed.stdout

  not_up = harbor_env.run("up", "nope")
  assert not_up.returncode == 1

  started = harbor_env.run("up", "ports-demo")
  assert started.returncode == 0, started.stderr
  assert "Running ports-demo" in started.stdout
  assert "LAN:" in started.stdout
  assert "harbor logs -f ports-demo" in started.stdout

  concise = harbor_env.run("ps")
  assert concise.returncode == 0, concise.stderr
  assert concise.stdout.splitlines()[0].split() == ["APP_ID", "STATUS", "LAST_ACTION"]
  concise_row = next(
    line for line in concise.stdout.splitlines() if line.startswith("ports-demo")
  )
  assert concise_row.split() == ["ports-demo", "running", "up"]

  stopped = harbor_env.run("down", "ports-demo")
  assert stopped.returncode == 0, stopped.stderr

  concise_stopped_row = next(
    line
    for line in harbor_env.run("ps").stdout.splitlines()
    if line.startswith("ports-demo")
  )
  assert concise_stopped_row.split() == ["ports-demo", "exited", "down"]

  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] in calls
  assert ["compose", "down"] in calls


def test_rm_data_removes_run_state_configuration_and_managed_volumes(harbor_env):
  app_id = "io.p2net.basic-features"
  upped = harbor_env.run("up", app_id, "--set", "admin_user=alice")
  assert upped.returncode == 0, upped.stderr
  assert (harbor_env.run_root / app_id).is_dir()
  assert (harbor_env.volumes_root / "data" / app_id / "config").is_dir()
  assert (harbor_env.volumes_root / "temp" / app_id / "cache").is_dir()

  removed = harbor_env.run("rm", app_id, "--data", "-y")
  assert removed.returncode == 0, removed.stderr

  assert not (harbor_env.run_root / app_id).exists()
  assert not (harbor_env.volumes_root / "data" / app_id).exists()
  assert not (harbor_env.volumes_root / "temp" / app_id).exists()
  db = harbor_env.read_db()
  assert app_id not in db.get("apps", {})


def test_invalid_manifest_is_rejected_before_up(harbor_env):
  app = harbor_env.root / "apps" / "invalid-mount.happ"
  app.mkdir()
  (app / "manifest.toml").write_text(
    """\
[app]
version = "1"

[run.main]
image = "alpine"
volumes = { missing = "/data" }
"""
  )

  result = harbor_env.run("up", "invalid-mount")

  assert result.returncode == 1
  assert "volume 'missing' is not declared" in result.stderr
  assert not (harbor_env.run_root / "invalid-mount").exists()


def test_missing_secret_blocks_up_with_recovery_command(harbor_env):
  app_id = "needs-secret"
  app = harbor_env.root / "apps" / f"{app_id}.happ"
  app.mkdir()
  (app / "manifest.toml").write_text(
    """\
[app]
version = "1"

[config]
api_key = { secret = true }

[run.main]
image = "alpine:latest"
cmd = ["true"]
"""
  )

  blocked = harbor_env.run("up", app_id)
  assert blocked.returncode == 1
  assert "Set with `harbor config`" in blocked.stderr
  assert (harbor_env.run_root / app_id).is_dir()

  needs_config_row = next(
    line for line in harbor_env.run("ps").stdout.splitlines() if line.startswith(app_id)
  )
  assert needs_config_row.split()[:3] == [app_id, "needs", "config"]

  configured = harbor_env.run("config", app_id, "--set", "api_key=sekrit")
  assert configured.returncode == 0, configured.stderr
  started = harbor_env.run("up", app_id)
  assert started.returncode == 0, started.stderr


def test_up_set_flag_one_shot(harbor_env):
  app_id = "io.p2net.basic-features"
  started = harbor_env.run("up", app_id, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr
  assert "Running" in started.stdout


def test_config_works_before_up(harbor_env):
  app_id = "io.p2net.basic-features"
  listed = harbor_env.run("config", app_id)
  assert listed.returncode == 0, listed.stderr
  assert "admin_user" in listed.stdout

  set_result = harbor_env.run("config", app_id, "--set", "admin_user=alice")
  assert set_result.returncode == 0, set_result.stderr

  path = harbor_env.root / "apps" / f"{app_id}.happ"
  path_list = harbor_env.run("config", str(path))
  assert path_list.returncode == 0, path_list.stderr


def test_config_set_secret(harbor_env):
  app_id = "io.p2net.basic-features"
  assert harbor_env.run("config", app_id, "--set", "admin_user=alice").returncode == 0

  missing = harbor_env.run("config", app_id, "--set", "admin_user")
  assert missing.returncode == 1
  assert "KEY=VALUE" in missing.stderr

  password = "hunter2"
  stored = harbor_env.run("config", app_id, "--set", f"admin_pass={password}")
  assert stored.returncode == 0, stored.stderr

  got = harbor_env.run("config", app_id, "--get", "admin_pass")
  assert got.returncode == 0, got.stderr
  assert got.stdout.strip() == "set"

  revealed = harbor_env.run("config", app_id, "--get", "admin_pass", "--show-secret")
  assert revealed.returncode == 0, revealed.stderr
  assert revealed.stdout.strip() == password

  listed = harbor_env.run("config", app_id)
  assert listed.returncode == 0, listed.stderr
  assert password not in listed.stdout
  assert "(secret)" in listed.stdout


def test_system_config_is_encrypted_listed_and_unset(harbor_env):
  route_key = "route_provider.nginx_proxy_manager.password"
  other_key = "backup.api_key"
  route_password = "correct horse battery staple"

  stored = harbor_env.run(
    "config-sys", "--stdin", route_key, input=f"{route_password}\n"
  )
  assert stored.returncode == 0, stored.stderr
  assert (
    harbor_env.run("config-sys", "--set", f"{other_key}=backup-secret").returncode == 0
  )

  db = harbor_env.read_db()
  encrypted = db["system"]["secrets"][route_key]
  assert encrypted != route_password
  assert route_password not in encrypted

  listed = harbor_env.run("config-sys")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.splitlines() == [other_key, route_key]
  assert route_password not in listed.stdout
  assert "backup-secret" not in listed.stdout

  unset = harbor_env.run("config-sys", "--unset", route_key)
  assert unset.returncode == 0, unset.stderr
  assert harbor_env.run("config-sys").stdout.splitlines() == [other_key]

  old_command = harbor_env.run("provider", "set-password")
  assert old_command.returncode == 2
  assert "invalid choice" in old_command.stderr


def test_external_bind_one_shot_via_up(harbor_env):
  app_id = "ext-volumes"
  blocked = harbor_env.run("up", app_id)
  assert blocked.returncode == 1
  assert "Bind with `harbor config <app_id> --bind`" in blocked.stderr

  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  started = harbor_env.run("up", app_id, "--bind", f"extvol1={host_volume}")
  assert started.returncode == 0, started.stderr
  link = harbor_env.run_root / app_id / "volumes" / "extvol1"
  assert link.is_symlink()
  assert link.resolve() == host_volume


def test_bind_then_up_without_restage(harbor_env):
  app_id = "ext-volumes"
  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  bound = harbor_env.run("config", app_id, "--bind", f"extvol1={host_volume}")
  assert bound.returncode == 0, bound.stderr
  started = harbor_env.run("up", app_id)
  assert started.returncode == 0, started.stderr


def test_missing_run_directory_with_container_refuses_lifecycle(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0
  shutil.rmtree(harbor_env.run_root / app_id)

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "run directory missing" in doctor.stderr
  assert "manual container recovery required" in doctor.stderr

  for command in (("down",), ("rm", "--runtime")):
    refused = harbor_env.run(*command, app_id)
    assert refused.returncode == 1
    assert "fake-container" in refused.stderr

  refused_rm = harbor_env.run("rm", app_id, "--data", "-y")
  assert refused_rm.returncode == 1
  assert "fake-container" in refused_rm.stderr
  # Port claims survive until a successful --data wipe; refused rm must not purge.
  assert app_id in harbor_env.read_db().get("routes", {})
  assert harbor_env.docker_state.exists()


def test_removed_app_bundle_remains_observable_and_stoppable(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0
  shutil.rmtree(harbor_env.root / "apps" / f"{app_id}.happ")

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "app bundle missing" in doctor.stderr

  stopped = harbor_env.run("down", app_id)
  assert stopped.returncode == 0, stopped.stderr

  assert (harbor_env.run_root / app_id / "source").is_symlink()
  started = harbor_env.run("up", app_id)
  assert started.returncode == 1
  assert f"Source for {app_id} is gone" in started.stderr


def test_up_by_id_records_source_link(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0

  link = harbor_env.run_root / app_id / "source"
  assert link.is_symlink()
  assert link.readlink() == (harbor_env.root / "apps" / f"{app_id}.happ").resolve()


def test_up_by_path_from_arbitrary_dir(harbor_env):
  app_id = "ports-demo"
  elsewhere = harbor_env.root / "elsewhere" / f"{app_id}.happ"
  shutil.copytree(harbor_env.root / "apps" / f"{app_id}.happ", elsewhere)
  shutil.rmtree(harbor_env.root / "apps" / f"{app_id}.happ")

  upped = harbor_env.run("up", str(elsewhere))
  assert upped.returncode == 0, upped.stderr
  link = harbor_env.run_root / app_id / "source"
  assert link.is_symlink()
  assert link.readlink() == elsewhere.resolve()

  stopped = harbor_env.run("down", app_id)
  assert stopped.returncode == 0, stopped.stderr
  assert harbor_env.run("up", str(elsewhere)).returncode == 0


def test_up_from_different_source_errors(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0
  assert harbor_env.run("down", app_id).returncode == 0

  other = harbor_env.root / "elsewhere" / f"{app_id}.happ"
  shutil.copytree(harbor_env.root / "apps" / f"{app_id}.happ", other)

  result = harbor_env.run("up", str(other))
  assert result.returncode == 1
  assert "already installed from" in result.stderr
  assert "harbor rm --runtime" in result.stderr


def test_up_invalid_path_arg_errors(harbor_env):
  not_happ = harbor_env.root / "not-a-happ"
  not_happ.mkdir()
  bad_suffix = harbor_env.run("up", str(not_happ))
  assert bad_suffix.returncode == 1
  assert "must end in .happ" in bad_suffix.stderr

  no_manifest = harbor_env.root / "empty.happ"
  no_manifest.mkdir()
  missing = harbor_env.run("up", str(no_manifest))
  assert missing.returncode == 1
  assert "missing manifest.toml" in missing.stderr

  absent = harbor_env.run("up", "./nope.happ")
  assert absent.returncode == 1
  assert "not a directory" in absent.stderr

  assert not (harbor_env.run_root / "empty").exists()


def test_up_unknown_id_errors(harbor_env):
  result = harbor_env.run("up", "nope")
  assert result.returncode == 1
  assert "No app found" in result.stderr


def test_rm_runtime_removes_run_dir_and_source_link(harbor_env):
  app_id = "ports-demo"
  bundle = harbor_env.root / "apps" / f"{app_id}.happ"
  assert harbor_env.run("up", app_id).returncode == 0
  assert harbor_env.run("down", app_id).returncode == 0
  assert (harbor_env.run_root / app_id / "source").is_symlink()

  assert harbor_env.run("rm", app_id, "--runtime").returncode == 0
  assert not (harbor_env.run_root / app_id).exists()
  assert bundle.is_dir()


def test_catalog_shows_available_apps_ps_hides_until_installed(harbor_env):
  app_id = "ports-demo"
  catalog = harbor_env.run("catalog")
  assert any(line.startswith(app_id) for line in catalog.stdout.splitlines())

  ps = harbor_env.run("ps")
  assert app_id not in ps.stdout

  listed = harbor_env.run("config", app_id)
  assert listed.returncode == 0, listed.stderr


def test_doctor_reports_abandoned_db(harbor_env):
  harbor_env.seed_db(
    {"apps/io.example.abandoned/config/admin_user": {"secret": False, "value": "x"}}
  )

  ps = harbor_env.run("ps")
  assert "io.example.abandoned" in ps.stdout

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "abandoned DB config" in doctor.stderr


def test_doctor_exposes_mixed_container_states(harbor_env):
  app_id = "io.example.docker-only"
  harbor_env.set_containers(
    [
      {
        "app_id": app_id,
        "run_unit": "main",
        "id": "running-container",
        "state": "running",
      },
      {
        "app_id": app_id,
        "run_unit": "worker",
        "id": "exited-container",
        "state": "exited",
      },
    ]
  )

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "mixed container states" in doctor.stderr


def test_missing_config_is_an_actionable_error(tmp_path):
  missing = tmp_path / "missing.toml"
  result = subprocess.run(
    [sys.executable, "-m", "harbor.cli", "ps"],
    env={**os.environ, "HARBOR_CONFIG": str(missing)},
    capture_output=True,
    text=True,
  )

  assert result.returncode == 1
  assert "Error: HARBOR_CONFIG is set" in result.stderr
  assert "Traceback" not in result.stderr


def test_curated_examples_materialize(harbor_env):
  examples = Path(__file__).parents[1] / "examples"
  for source in examples.glob("*.happ"):
    shutil.copytree(source, harbor_env.root / "apps" / source.name, dirs_exist_ok=True)
    fresh = HarborCtx(load_config_file(harbor_env.config, "test"))
    app = fresh.resolve_app(source.stem)
    lifecycle.stage(app, fresh, fresh.bundle_path(app))
    assert (harbor_env.run_root / source.stem / "compose.yml").is_file()


class _RecordingRouteProvider:
  def __init__(self, owners: dict[str, str] | None = None):
    self.registered = []
    self.unregistered = []
    self._owners = owners or {}

  def register_route(self, app, port, subdomain, domain, scheme="http"):
    self.registered.append((app, port, subdomain, domain, scheme))

  def unregister_route(self, subdomain, domain):
    self.unregistered.append((subdomain, domain))

  def route_owners(self):
    return self._owners


def test_duplicate_fqdn_is_rejected_before_compose_up(harbor_env, monkeypatch):
  # Provider already owns photos.* under another happ — start must refuse before compose.
  provider = _RecordingRouteProvider(
    owners={"photos": "other-routes", "api-photos": "other-routes"}
  )
  monkeypatch.setattr(lifecycle, "get_route_provider", lambda db, config: provider)
  monkeypatch.setattr("harbor.lib.harbor.load_harbor_run_unit_status", lambda: {})
  docker_calls = []
  monkeypatch.setattr(
    lifecycle,
    "docker_run_command",
    lambda args, **kwargs: docker_calls.append(args) or "",
  )
  ctx = HarborCtx(load_config_file(harbor_env.config, "test"))

  app = ctx.resolve_app("routes-demo")
  lifecycle.stage(app, ctx, ctx.bundle_path(app))

  start_ctx = HarborCtx(load_config_file(harbor_env.config, "test"))
  with pytest.raises(ValueError, match="already owned"):
    lifecycle.start(app, start_ctx)
  assert provider.registered == []
  assert ["compose", "up", "-d"] not in docker_calls


def test_down_uses_staged_manifest_when_bundle_is_missing(harbor_env, monkeypatch):
  provider = _RecordingRouteProvider()
  monkeypatch.setattr(lifecycle, "get_route_provider", lambda db, config: provider)
  monkeypatch.setattr("harbor.lib.harbor.load_harbor_run_unit_status", lambda: {})
  monkeypatch.setattr(lifecycle, "docker_run_command", lambda args, **kwargs: "")
  stage_ctx = HarborCtx(load_config_file(harbor_env.config, "test"))
  app = stage_ctx.resolve_app("routes-demo")
  lifecycle.stage(app, stage_ctx, stage_ctx.bundle_path(app))
  start_ctx = HarborCtx(load_config_file(harbor_env.config, "test"))
  lifecycle.start(app, start_ctx)
  shutil.rmtree(harbor_env.root / "apps" / "routes-demo.happ")

  fresh_ctx = HarborCtx(load_config_file(harbor_env.config, "test"))
  lifecycle.stop("routes-demo", fresh_ctx)
  assert provider.unregistered == [
    ("photos", "harbor.localhost"),
    ("api-photos", "harbor.localhost"),
  ]


def test_readme_quickstart_from_repo_examples(harbor_env):
  """Getting-started path from a checkout: up an apps/ happ by path, then ps.

  Deliberately a happ shipped in the repo rather than a tests/fixtures one:
  this is the path a reader follows straight from the README, so it should
  break if the example happs do. `demo-routes` because its `lan_only` route
  keeps the LAN receipt line under test.
  """
  happ = Path(__file__).parents[1] / "apps" / "demo-routes.happ"
  assert happ.is_dir(), happ

  started = harbor_env.run("up", str(happ))
  assert started.returncode == 0, started.stderr
  assert "Running demo-routes" in started.stdout
  assert "LAN:" in started.stdout

  ps = harbor_env.run("ps")
  assert ps.returncode == 0, ps.stderr
  assert "demo-routes" in ps.stdout


def test_status_and_inspect(harbor_env):
  assert harbor_env.run("up", "ports-demo").returncode == 0
  status = harbor_env.run("status", "ports-demo")
  assert status.returncode == 0, status.stderr
  assert "running" in status.stdout
  assert "LAN:" in status.stdout
  assert "harbor logs -f ports-demo" in status.stdout

  inspected = harbor_env.run("inspect", "ports-demo")
  assert inspected.returncode == 0, inspected.stderr
  assert "Images:" in inspected.stdout
  assert "alpine:latest" in inspected.stdout


def test_config_set_while_running_warns(harbor_env):
  app_id = "io.p2net.basic-features"
  assert harbor_env.run("up", app_id, "--set", "admin_user=alice").returncode == 0
  result = harbor_env.run("config", app_id, "--set", "admin_user=bob")
  assert result.returncode == 0, result.stderr
  assert "is running" in result.stderr
  assert f"harbor down {app_id}" in result.stderr
  assert f"harbor up {app_id}" in result.stderr


def test_logs_accepts_native_flags_before_app(harbor_env):
  assert harbor_env.run("up", "ports-demo").returncode == 0
  # Fake docker ignores unknown compose args; success means argparse accepted order.
  result = harbor_env.run("logs", "-f", "--tail", "10", "ports-demo")
  assert result.returncode == 0, result.stderr
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "logs", "--follow", "--tail", "10"] in calls


# --- locking ---------------------------------------------------------------
#
# Harbor takes one lock per command invocation: `harbor/cli/main.py` wraps the
# whole command in `ctx.lock()`. `init` runs outside it, since there is no
# config to load yet -- which is exactly the path that had no test and shipped
# broken.


def test_init_bootstraps_a_usable_root(harbor_env, tmp_path):
  """`harbor init` runs before any config or lock exists.

  Nothing else in the suite calls it, because every other test starts from a
  root the fixture builds by hand.
  """
  root = tmp_path / "fresh"
  # init prompts for the root; an empty line accepts the --root default.
  result = harbor_env.run("--root", str(root), "init", input="\n")

  assert result.returncode == 0, result.stderr
  assert (root / "config.toml").is_file()
  assert (root / "master.key").is_file()
  assert (root / "apps").is_dir()
  assert (root / "run").is_dir()
  for kind in VOLUME_KINDS:
    assert (root / "volumes" / kind).is_dir(), kind

  # The master key must be readable back, not merely present: a command against
  # the new root has to load it through load_config_file.
  after = harbor_env.run("--root", str(root), "ps")
  assert after.returncode == 0, after.stderr


def test_gen_masterkey_appends_to_the_keyfile(harbor_env):
  before = (harbor_env.root / "master.key").read_text()
  result = harbor_env.run("config-sys", "gen-masterkey")

  assert result.returncode == 0, result.stderr
  after = (harbor_env.root / "master.key").read_text()
  assert after.startswith(before) and len(after) > len(before)


def test_the_lock_is_recorded_then_released(harbor_env):
  """A held lock and a released one are both visible in the activity log."""
  ctx = HarborCtx(load_config_file(harbor_env.config, "test"))
  activity = LogTab(ctx.config.activity_log)

  with ctx.lock("ps"):
    held = json.loads(activity.read(LOCK_KEY))
    assert held["state"] == "acquired"
    assert held["by"] == "ps"
    assert held["pid"] == os.getpid()

  released = json.loads(activity.read(LOCK_KEY))
  assert released["state"] == "released"
  assert released["by"] == "ps"


def test_nested_locks_record_only_the_outermost(harbor_env):
  """Reentrancy must not log a release while the lock is still held."""
  ctx = HarborCtx(load_config_file(harbor_env.config, "test"))
  activity = LogTab(ctx.config.activity_log)

  with ctx.lock("outer"):
    with ctx.lock("inner"):
      pass
    still_held = json.loads(activity.read(LOCK_KEY))
    assert still_held["state"] == "acquired"
    assert still_held["by"] == "outer"

  assert json.loads(activity.read(LOCK_KEY))["state"] == "released"


def test_the_lock_timeout_message_names_the_holder(harbor_env):
  """The whole point of the record: explain a wait instead of just failing."""
  config = load_config_file(harbor_env.config, "test")
  LogTab(config.activity_log).write(
    LOCK_KEY,
    json.dumps(
      {
        "state": "acquired",
        "by": "up ports-demo",
        "pid": 999999,
        "at": "2026-07-29T18:22:04-06:00",
      }
    ),
  )

  with FileLock(harbor_env.harbor_lockfile_path):
    blocked = harbor_env.run("ps", timeout=LOCK_TIMEOUT + 20)

  assert blocked.returncode == 1
  assert "up ports-demo" in blocked.stderr
  assert "999999" in blocked.stderr


def test_the_recorded_holder_is_the_command_not_the_argv(harbor_env):
  """`config --set k=secret` must never put the value in the activity log."""
  app_id = "io.p2net.basic-features"
  assert harbor_env.run("config", app_id, "--set", "admin_pass=hunter2").returncode == 0

  config = load_config_file(harbor_env.config, "test")
  recorded = LogTab(config.activity_log).read(LOCK_KEY)

  assert "hunter2" not in recorded
  assert json.loads(recorded)["by"] == f"config {app_id}"


def test_a_command_holds_the_harbor_lock(harbor_env):
  """One lock per invocation, so a second harbor waits on the first."""
  lock = FileLock(harbor_env.harbor_lockfile_path)

  # Free when nothing is running.
  lock.acquire(timeout=0)
  lock.release()

  # Held by us, harbor must wait rather than run concurrently -- and then give
  # up, rather than hang forever with nothing on screen to explain why.
  with lock:
    result = harbor_env.run("ps", timeout=LOCK_TIMEOUT + 20)

  assert result.returncode == 1
  assert "Another process has locked harbor" in result.stderr
  assert f"{LOCK_TIMEOUT:g} seconds" in result.stderr

  # ...and it proceeds again once released.
  assert harbor_env.run("ps", timeout=30).returncode == 0


def test_waiting_for_the_lock_is_bounded(harbor_env):
  """The wait must actually end on its own, not just eventually."""
  with FileLock(harbor_env.harbor_lockfile_path):
    started = time.monotonic()
    result = harbor_env.run("ps", timeout=LOCK_TIMEOUT + 20)
    waited = time.monotonic() - started

  assert result.returncode == 1
  assert LOCK_TIMEOUT <= waited < LOCK_TIMEOUT + 15, waited


def test_last_action_is_read_in_one_pass(harbor_env):
  """`ps` reports every app's action from a single read of the shared log."""
  assert harbor_env.run("up", "ports-demo").returncode == 0
  assert harbor_env.run("up", "routes-demo").returncode == 0

  config = load_config_file(harbor_env.config, "test")
  assert read_app_actions(config) == {"ports-demo": "up", "routes-demo": "up"}

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.count("up") >= 2


def test_purged_is_recorded_when_an_app_is_removed(harbor_env):
  """The activity log outlives the app, so removal is recorded, not erased."""
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0

  config = load_config_file(harbor_env.config, "test")
  assert read_last_app_action(app_id, config) == "up"

  assert harbor_env.run("rm", app_id, "--data", "-y").returncode == 0

  assert not (harbor_env.run_root / app_id).exists()
  assert read_last_app_action(app_id, config) == "purged"
  assert f"{app_id}/status" in LogTab(config.activity_log).load()


# --- ps status accuracy ----------------------------------------------------
#
# `up` establishes preconditions before it judges readiness: stage() generates
# config defaults and reallocates every route, *then* evaluates blockers. `ps`
# calls load_run_data() with neither having run, so anything up repairs itself
# must not be reported as something the operator has to fix.


def test_unallocated_routes_are_not_reported_as_needing_config(harbor_env):
  """The reported bug: `ps` said "needs config" for an app needing none."""
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0
  assert harbor_env.run("down", app_id).returncode == 0

  # Route rows gone while the run dir survives -- the pre-`up` allocation state.
  config = load_config_file(harbor_env.config, "test")
  HarborDB.from_config(config).app_db(app_id).clear_routes()
  assert (harbor_env.run_root / app_id / "compose.yml").is_file()

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert "needs config" not in listed.stdout, listed.stdout

  # ...and it is genuinely startable, configuring nothing.
  assert harbor_env.run("up", app_id).returncode == 0


def test_an_unmet_bind_is_still_reported_as_needing_config(harbor_env):
  """The label must keep working for the case it actually describes.

  An ext volume whose host path has gone is the operator's to fix -- `up`
  cannot invent it -- so this one really does need `harbor config --bind`.
  """
  app_id = "ext-volumes"
  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  assert (
    harbor_env.run("config", app_id, "--bind", f"extvol1={host_volume}").returncode == 0
  )
  assert harbor_env.run("up", app_id).returncode == 0
  assert harbor_env.run("down", app_id).returncode == 0

  shutil.rmtree(host_volume)

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert "needs config" in listed.stdout, listed.stdout


def test_an_unloadable_app_is_not_reported_as_needing_config(harbor_env):
  """A moved happ directory is unreadable, not unconfigured."""
  app_id = "ports-demo"
  assert harbor_env.run("up", app_id).returncode == 0
  assert harbor_env.run("down", app_id).returncode == 0

  bundle = harbor_env.root / "apps" / f"{app_id}.happ"
  bundle.rename(bundle.with_suffix(".moved"))

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert "unreadable" in listed.stdout, listed.stdout
  assert "needs config" not in listed.stdout, listed.stdout


def test_up_blocks_until_a_required_config_value_is_set(harbor_env):
  """A value with no default must be supplied, whether or not it is secret.

  `admin_user` is declared `{}` in the fixture -- required, non-secret. It was
  silently treated as satisfied, so an app could start with it empty.
  """
  app_id = "io.p2net.basic-features"

  blocked = harbor_env.run("up", app_id)
  assert blocked.returncode == 1, blocked.stdout
  assert "admin_user" in blocked.stderr
  assert "Set with `harbor config`" in blocked.stderr
  # stage() materializes before start_blockers are judged, so the run dir
  # exists; what must not have happened is the container starting.
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] not in calls

  started = harbor_env.run("up", app_id, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr


def test_a_required_config_value_shows_as_needing_config(harbor_env):
  """The non-secret counterpart of test_missing_secret_blocks_up_...

  A dedicated happ whose only config is non-secret and default-less, so the
  blocker cannot be attributed to some other value.
  """
  app_id = "needs-value"
  app = harbor_env.root / "apps" / f"{app_id}.happ"
  app.mkdir()
  (app / "manifest.toml").write_text(
    """\
[app]
version = "1"

[config]
hostname = {}

[run.main]
image = "alpine:latest"
cmd = ["true"]
"""
  )

  blocked = harbor_env.run("up", app_id)
  assert blocked.returncode == 1
  assert "hostname is unset and no default specified" in blocked.stderr
  assert "Set with `harbor config`" in blocked.stderr

  row = next(
    line for line in harbor_env.run("ps").stdout.splitlines() if line.startswith(app_id)
  )
  assert row.split()[:3] == [app_id, "needs", "config"]

  assert harbor_env.run("config", app_id, "--set", "hostname=box").returncode == 0
  started = harbor_env.run("up", app_id)
  assert started.returncode == 0, started.stderr


def test_logs_does_not_hold_the_harbor_lock(harbor_env):
  """`logs -f` streams until interrupted.

  Holding the lock for that long would lock the operator out of harbor for as
  long as they watch logs, so this command runs unlocked.
  """
  assert harbor_env.run("up", "ports-demo").returncode == 0

  with FileLock(harbor_env.harbor_lockfile_path):
    tailed = harbor_env.run("logs", "ports-demo", timeout=LOCK_TIMEOUT + 20)
    # ...while a state-changing command still waits and gives up.
    blocked = harbor_env.run("ps", timeout=LOCK_TIMEOUT + 20)

  assert tailed.returncode == 0, tailed.stderr
  assert "locked harbor" not in tailed.stderr
  assert blocked.returncode == 1
  assert "Another process has locked harbor" in blocked.stderr
