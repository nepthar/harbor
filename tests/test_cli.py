"""The harbor command surface, driven through `cli.main.run` in-process.

Each test asserts on what an operator sees -- exit code, stdout, stderr -- plus
the state on disk it claims to have changed. Anything that needs a real docker
daemon, a real route provider, or root-owned volume data is in docs/testing.md
instead.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from harbor.lib import lifecycle
from harbor.lib.apps import read_app_actions, read_last_app_action
from harbor.lib.config import VOLUME_KINDS, load_config_file
from harbor.lib.happ import scan_happs
from harbor.lib.harbor import HarborCtx
from harbor.lib.logtab import LogTab
from harbor.lib.store import HarborStore

BASIC = "io.p2net.basic-features"


# --- lifecycle -------------------------------------------------------------


def test_start_materializes_compose_and_port_state(harbor_env):
  result = harbor_env.run("start", "ports-demo")

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


def test_start_ps_stop_tracks_docker_reality(harbor_env):
  catalog = harbor_env.run("catalog")
  assert catalog.returncode == 0, catalog.stderr
  assert "ports-demo" in catalog.stdout

  not_installed = harbor_env.run("ps")
  assert not_installed.returncode == 0
  assert "ports-demo" not in not_installed.stdout

  started = harbor_env.run("start", "ports-demo")
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
  assert concise_row.split() == ["ports-demo", "running", "started"]

  stopped = harbor_env.run("stop", "ports-demo")
  assert stopped.returncode == 0, stopped.stderr

  concise_stopped_row = next(
    line
    for line in harbor_env.run("ps").stdout.splitlines()
    if line.startswith("ports-demo")
  )
  assert concise_stopped_row.split() == ["ports-demo", "exited", "stopped"]

  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] in calls
  assert ["compose", "down"] in calls


def test_rm_removes_run_state_configuration_and_managed_volumes(harbor_env):
  started = harbor_env.run("start", BASIC, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr
  assert (harbor_env.run_root / BASIC).is_dir()
  assert (harbor_env.volumes_root / "data" / BASIC / "config").is_dir()
  assert (harbor_env.volumes_root / "temp" / BASIC / "cache").is_dir()

  removed = harbor_env.run("rm", BASIC, "-y")
  assert removed.returncode == 0, removed.stderr

  assert not (harbor_env.run_root / BASIC).exists()
  assert not (harbor_env.volumes_root / "data" / BASIC).exists()
  assert not (harbor_env.volumes_root / "temp" / BASIC).exists()
  assert BASIC not in harbor_env.read_db().get("routes", {})


def test_rm_leaves_the_catalog_entry_alone(harbor_env):
  app_id = "ports-demo"
  bundle = harbor_env.root / "apps" / f"{app_id}.happ"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  assert harbor_env.run("rm", app_id, "-y").returncode == 0
  assert not (harbor_env.run_root / app_id).exists()
  assert bundle.is_dir()


def test_status_and_inspect(harbor_env):
  assert harbor_env.run("start", "ports-demo").returncode == 0
  status = harbor_env.run("status", "ports-demo")
  assert status.returncode == 0, status.stderr
  assert "running" in status.stdout
  assert "LAN:" in status.stdout
  assert "harbor logs -f ports-demo" in status.stdout

  inspected = harbor_env.run("inspect", "ports-demo")
  assert inspected.returncode == 0, inspected.stderr
  assert "Images:" in inspected.stdout
  assert "alpine:latest" in inspected.stdout


def test_catalog_shows_available_apps_ps_hides_until_installed(harbor_env):
  app_id = "ports-demo"
  catalog = harbor_env.run("catalog")
  assert any(line.startswith(app_id) for line in catalog.stdout.splitlines())

  ps = harbor_env.run("ps")
  assert app_id not in ps.stdout

  # Unstaged apps have no run/ copy; inspect a path instead of the catalog id.
  happ = harbor_env.root / "apps" / f"{app_id}.happ"
  inspected = harbor_env.run("inspect", str(happ))
  assert inspected.returncode == 0, inspected.stderr


def test_logs_accepts_native_flags_before_app(harbor_env):
  assert harbor_env.run("start", "ports-demo").returncode == 0
  # Fake docker ignores unknown compose args; success means argparse accepted order.
  result = harbor_env.run("logs", "-f", "--tail", "10", "ports-demo")
  assert result.returncode == 0, result.stderr
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "logs", "--follow", "--tail", "10"] in calls


# --- refusals --------------------------------------------------------------


def test_invalid_manifest_is_rejected_before_start(harbor_env):
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

  result = harbor_env.run("start", "invalid-mount")

  assert result.returncode == 1
  assert "volume 'missing' is not declared" in result.stderr
  assert not (harbor_env.run_root / "invalid-mount").exists()


def test_start_unknown_id_errors(harbor_env):
  result = harbor_env.run("start", "nope")
  assert result.returncode == 1
  assert "No app found" in result.stderr


def test_start_invalid_path_arg_errors(harbor_env):
  not_happ = harbor_env.root / "not-a-happ"
  not_happ.mkdir()
  bad_suffix = harbor_env.run("start", str(not_happ))
  assert bad_suffix.returncode == 1
  assert "must end in .happ" in bad_suffix.stderr

  no_manifest = harbor_env.root / "empty.happ"
  no_manifest.mkdir()
  missing = harbor_env.run("start", str(no_manifest))
  assert missing.returncode == 1
  assert "missing manifest.toml" in missing.stderr

  absent = harbor_env.run("start", "./nope.happ")
  assert absent.returncode == 1
  assert "not a directory" in absent.stderr

  assert not (harbor_env.run_root / "empty").exists()


def test_start_by_path_from_arbitrary_dir(harbor_env):
  app_id = "ports-demo"
  elsewhere = harbor_env.root / "elsewhere" / f"{app_id}.happ"
  shutil.copytree(harbor_env.root / "apps" / f"{app_id}.happ", elsewhere)
  shutil.rmtree(harbor_env.root / "apps" / f"{app_id}.happ")

  started = harbor_env.run("start", str(elsewhere))
  assert started.returncode == 0, started.stderr
  entry = harbor_env.root / "apps" / f"{app_id}.happ"
  assert entry.is_symlink()
  assert entry.readlink() == elsewhere.resolve()

  stopped = harbor_env.run("stop", app_id)
  assert stopped.returncode == 0, stopped.stderr
  assert harbor_env.run("start", str(elsewhere)).returncode == 0


def test_start_from_a_conflicting_path_is_refused(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  other = harbor_env.root / "elsewhere" / f"{app_id}.happ"
  shutil.copytree(harbor_env.root / "apps" / f"{app_id}.happ", other)

  result = harbor_env.run("start", str(other))
  assert result.returncode == 1
  assert "already in the catalog" in result.stderr


def test_missing_run_directory_with_container_refuses_lifecycle(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  shutil.rmtree(harbor_env.run_root / app_id)

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "run directory missing" in doctor.stderr
  assert "manual container recovery required" in doctor.stderr

  for command in (("stop",), ("rm", "-y")):
    refused = harbor_env.run(*command, app_id)
    assert refused.returncode == 1
    assert "fake-container" in refused.stderr

  # Port claims survive a refused rm; nothing is purged.
  assert app_id in harbor_env.read_db().get("routes", {})
  assert harbor_env.docker_state.exists()


def test_removed_app_bundle_remains_runnable_from_the_staged_copy(harbor_env):
  """The run copy is what harbor runs, so deleting apps/<id>.happ is survivable.

  It still shows up as a problem in `doctor` -- nothing can re-stage the app
  until the catalog entry is back -- but stop and start keep working.
  """
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  shutil.rmtree(harbor_env.root / "apps" / f"{app_id}.happ")

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "app bundle missing" in doctor.stderr

  stopped = harbor_env.run("stop", app_id)
  assert stopped.returncode == 0, stopped.stderr

  restarted = harbor_env.run("start", app_id)
  assert restarted.returncode == 0, restarted.stderr

  restaged = harbor_env.run("stage", app_id)
  assert restaged.returncode == 1
  assert "No app found" in restaged.stderr


def test_missing_config_is_an_actionable_error(harbor_env, monkeypatch, tmp_path):
  monkeypatch.setenv("HARBOR_CONFIG", str(tmp_path / "missing.toml"))

  result = harbor_env.run("ps")

  assert result.returncode == 1
  assert "Error: HARBOR_CONFIG is set" in result.stderr
  assert "Traceback" not in result.stderr


# --- config ----------------------------------------------------------------


def test_config_requires_staging_first(harbor_env):
  """Config lives in the run dir, so there is nowhere to put it until staged."""
  too_early = harbor_env.run("config", BASIC, "--set", "admin_user=alice")
  assert too_early.returncode == 1
  assert f"harbor stage {BASIC}" in too_early.stderr

  assert harbor_env.run("stage", BASIC).returncode == 0

  listed = harbor_env.run("config", BASIC)
  assert listed.returncode == 0, listed.stderr
  assert "admin_user" in listed.stdout

  set_result = harbor_env.run("config", BASIC, "--set", "admin_user=alice")
  assert set_result.returncode == 0, set_result.stderr


def test_config_set_secret(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  assert harbor_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0

  missing = harbor_env.run("config", BASIC, "--set", "admin_user")
  assert missing.returncode == 1
  assert "KEY=VALUE" in missing.stderr

  password = "hunter2"
  stored = harbor_env.run("config", BASIC, "--set", f"admin_pass={password}")
  assert stored.returncode == 0, stored.stderr

  got = harbor_env.run("config", BASIC, "--get", "admin_pass")
  assert got.returncode == 0, got.stderr
  assert got.stdout.strip() == "set"

  revealed = harbor_env.run("config", BASIC, "--get", "admin_pass", "--show-secret")
  assert revealed.returncode == 0, revealed.stderr
  assert revealed.stdout.strip() == password

  listed = harbor_env.run("config", BASIC)
  assert listed.returncode == 0, listed.stderr
  assert password not in listed.stdout
  assert "(secret)" in listed.stdout


def test_config_set_while_running_warns(harbor_env):
  assert harbor_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  result = harbor_env.run("config", BASIC, "--set", "admin_user=bob")
  assert result.returncode == 0, result.stderr
  assert "is running" in result.stderr
  assert f"harbor stop {BASIC}" in result.stderr
  assert f"harbor start {BASIC}" in result.stderr


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


def test_external_bind_one_shot_via_start(harbor_env):
  app_id = "ext-volumes"
  blocked = harbor_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "Bind with `harbor config <app_id> --bind`" in blocked.stderr

  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  started = harbor_env.run("start", app_id, "--bind", f"extvol1={host_volume}")
  assert started.returncode == 0, started.stderr
  link = harbor_env.run_root / app_id / "volumes" / "ext" / "extvol1"
  assert link.is_symlink()
  assert link.resolve() == host_volume


def test_bind_then_start_without_explicit_restage(harbor_env):
  app_id = "ext-volumes"
  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  assert harbor_env.run("stage", app_id).returncode == 0
  bound = harbor_env.run("config", app_id, "--bind", f"extvol1={host_volume}")
  assert bound.returncode == 0, bound.stderr
  started = harbor_env.run("start", app_id)
  assert started.returncode == 0, started.stderr


# --- start blockers --------------------------------------------------------


def test_missing_secret_blocks_start_with_recovery_command(harbor_env):
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

  blocked = harbor_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "Set with `harbor config`" in blocked.stderr
  assert (harbor_env.run_root / app_id).is_dir()

  needs_config_row = next(
    line for line in harbor_env.run("ps").stdout.splitlines() if line.startswith(app_id)
  )
  assert needs_config_row.split()[:3] == [app_id, "needs", "config"]

  configured = harbor_env.run("config", app_id, "--set", "api_key=sekrit")
  assert configured.returncode == 0, configured.stderr
  started = harbor_env.run("start", app_id)
  assert started.returncode == 0, started.stderr


def test_a_required_non_secret_value_blocks_start_and_shows_as_needing_config(
  harbor_env,
):
  """The non-secret counterpart of the missing-secret case.

  A value with no default must be supplied whether or not it is secret; this
  was once silently treated as satisfied, so an app could start with it empty.
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

  blocked = harbor_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "hostname is unset and no default specified" in blocked.stderr
  assert "Set with `harbor config`" in blocked.stderr
  # stage() materializes before start_blockers are judged, so the run dir
  # exists; what must not have happened is the container starting.
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] not in calls

  row = next(
    line for line in harbor_env.run("ps").stdout.splitlines() if line.startswith(app_id)
  )
  assert row.split()[:3] == [app_id, "needs", "config"]

  assert harbor_env.run("config", app_id, "--set", "hostname=box").returncode == 0
  assert harbor_env.run("start", app_id).returncode == 0


# --- ps status accuracy ----------------------------------------------------
#
# `start` establishes preconditions before it judges readiness: stage() generates
# config defaults and reallocates every route, *then* evaluates blockers. `ps`
# calls load_run_data() with neither having run, so anything start repairs itself
# must not be reported as something the operator has to fix.


def test_unallocated_routes_are_not_reported_as_needing_config(harbor_env):
  """The reported bug: `ps` said "needs config" for an app needing none."""
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  # Route rows gone while the run dir survives -- the pre-start allocation state.
  config = load_config_file(harbor_env.config)
  HarborStore.from_config(config).clear_routes(app_id)
  assert (harbor_env.run_root / app_id / "compose.yml").is_file()

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert "needs config" not in listed.stdout, listed.stdout

  # ...and it is genuinely startable, configuring nothing.
  assert harbor_env.run("start", app_id).returncode == 0


def test_an_unmet_bind_is_still_reported_as_needing_config(harbor_env):
  """The label must keep working for the case it actually describes.

  An ext volume whose host path has gone is the operator's to fix -- `start`
  cannot invent it -- so this one really does need `harbor config --bind`.
  """
  app_id = "ext-volumes"
  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  assert harbor_env.run("stage", app_id).returncode == 0
  assert (
    harbor_env.run("config", app_id, "--bind", f"extvol1={host_volume}").returncode == 0
  )
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_volume)

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert "needs config" in listed.stdout, listed.stdout


def test_an_unloadable_app_is_not_reported_as_needing_config(harbor_env):
  """A staged happ that will not parse is unreadable, not unconfigured."""
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  (harbor_env.run_root / app_id / "happ" / "manifest.toml").write_text("not toml {[")

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert "unreadable" in listed.stdout, listed.stdout
  assert "needs config" not in listed.stdout, listed.stdout


# --- doctor ----------------------------------------------------------------


def test_doctor_reports_orphaned_routes(harbor_env):
  """Routes are the only per-app state harbordb still holds.

  Config and binds live in the run directory now, so a harbordb entry with no
  run directory can only be a route allocation nothing owns -- which still
  pins a host port and so is worth reporting.
  """
  harbor_env.seed_db(
    {
      "routes/io.example.abandoned/web": {
        "name": "web",
        "subdomain": "",
        "run_unit_name": "main",
        "host_port": 41000,
        "container_port": 8080,
        "proto": "tcp",
        "publish": "lan",
        "scheme": "http",
      }
    }
  )

  ps = harbor_env.run("ps")
  assert "io.example.abandoned" in ps.stdout

  doctor = harbor_env.run("doctor")
  assert doctor.returncode == 1
  assert "orphaned route allocation" in doctor.stderr


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


# --- route provider --------------------------------------------------------
#
# The provider is stubbed here. Registering against a real Nginx Proxy Manager
# is a live test; what these pin down is *when* harbor calls it.


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


@pytest.fixture
def stub_provider(monkeypatch):
  def install(owners: dict[str, str] | None = None) -> _RecordingRouteProvider:
    provider = _RecordingRouteProvider(owners)
    monkeypatch.setattr(
      "harbor.lib.lifecycle.routes.get_route_provider",
      lambda db, config: provider,
    )
    monkeypatch.setattr("harbor.lib.harbor.load_harbor_run_unit_status", lambda: {})
    return provider

  return install


def test_duplicate_fqdn_is_rejected_before_compose_up(
  harbor_env, monkeypatch, stub_provider
):
  # Provider already owns photos.* under another happ -- start must refuse
  # before compose.
  provider = stub_provider({"photos": "other-routes", "api-photos": "other-routes"})
  docker_calls = []
  monkeypatch.setattr(
    "harbor.lib.lifecycle.run.docker_run_command",
    lambda args, **kwargs: docker_calls.append(args) or "",
  )
  ctx = HarborCtx(load_config_file(harbor_env.config))

  app = ctx.resolve_app("routes-demo")
  lifecycle.stage(app, ctx.bundle_path(app), ctx)

  start_ctx = HarborCtx(load_config_file(harbor_env.config))
  with pytest.raises(ValueError, match="already owned"):
    lifecycle.start(app, start_ctx.bundle_path(app), start_ctx)
  assert provider.registered == []
  assert ["compose", "up", "-d"] not in docker_calls


def test_stop_uses_staged_manifest_when_bundle_is_missing(
  harbor_env, monkeypatch, stub_provider
):
  provider = stub_provider()
  monkeypatch.setattr(
    "harbor.lib.lifecycle.run.docker_run_command",
    lambda args, **kwargs: "",
  )
  stage_ctx = HarborCtx(load_config_file(harbor_env.config))
  app = stage_ctx.resolve_app("routes-demo")
  lifecycle.stage(app, stage_ctx.bundle_path(app), stage_ctx)
  start_ctx = HarborCtx(load_config_file(harbor_env.config))
  lifecycle.start(app, start_ctx.bundle_path(app), start_ctx)
  shutil.rmtree(harbor_env.root / "apps" / "routes-demo.happ")

  fresh_ctx = HarborCtx(load_config_file(harbor_env.config))
  lifecycle.stop("routes-demo", fresh_ctx)
  assert provider.unregistered == [
    ("photos", "harbor.localhost"),
    ("api-photos", "harbor.localhost"),
  ]


# --- bootstrap -------------------------------------------------------------


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


def test_every_shipped_happ_stages(harbor_env):
  """The happs in apps/ are what a reader installs first; they must parse.

  This previously pointed at an `examples/` directory that does not exist, so
  it globbed nothing and passed unconditionally.
  """
  apps_dir = Path(__file__).parents[1] / "apps"
  shipped = list(scan_happs(apps_dir))
  assert shipped, "no happs found in apps/"

  for app_id, rel_path in shipped:
    source = apps_dir / rel_path
    dest = harbor_env.root / "apps" / source.name
    if source.is_dir():
      shutil.copytree(source, dest, dirs_exist_ok=True)
    else:
      shutil.copy2(source, dest)
    fresh = HarborCtx(load_config_file(harbor_env.config))
    app = fresh.resolve_app(app_id)
    lifecycle.stage(app, fresh.bundle_path(app), fresh)
    assert (harbor_env.run_root / app_id / "compose.yml").is_file(), source.name


def test_readme_quickstart_from_repo_apps(harbor_env):
  """Getting-started path from a checkout: start an apps/ happ by path, then ps.

  Deliberately a happ shipped in the repo rather than a tests/fixtures one:
  this is the path a reader follows straight from the README, so it should
  break if the shipped happs do. `demo-routes` because its `lan_only` route
  keeps the LAN receipt line under test.
  """
  happ = Path(__file__).parents[1] / "apps" / "demo-routes.happ.md"
  assert happ.is_file(), happ

  started = harbor_env.run("start", str(happ))
  assert started.returncode == 0, started.stderr
  assert "Running demo-routes" in started.stdout
  assert "LAN:" in started.stdout

  ps = harbor_env.run("ps")
  assert ps.returncode == 0, ps.stderr
  assert "demo-routes" in ps.stdout


# --- activity log ----------------------------------------------------------


def test_last_action_is_read_in_one_pass(harbor_env):
  """`ps` reports every app's action from a single read of the shared log."""
  assert harbor_env.run("start", "ports-demo").returncode == 0
  assert harbor_env.run("start", "routes-demo").returncode == 0

  config = load_config_file(harbor_env.config)
  assert read_app_actions(config) == {
    "ports-demo": "started",
    "routes-demo": "started",
  }

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.count("started") >= 2


def test_removal_is_recorded_when_an_app_is_removed(harbor_env):
  """The activity log outlives the app, so removal is recorded, not erased."""
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0

  config = load_config_file(harbor_env.config)
  assert read_last_app_action(app_id, config) == "started"

  assert harbor_env.run("rm", app_id, "-y").returncode == 0

  assert not (harbor_env.run_root / app_id).exists()
  assert read_last_app_action(app_id, config) == "removed"
  assert f"{app_id}/status" in LogTab(config.activity_log).load()
