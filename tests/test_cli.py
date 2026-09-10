"""The kelso command surface, driven through `cli.main.run` in-process.

Each test asserts on what an operator sees -- exit code, stdout, stderr -- plus
the state on disk it claims to have changed. Anything that needs a real docker
daemon, a real route provider, or root-owned volume data is in docs/testing.md
instead.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from kelso.lib import lifecycle
from kelso.lib.apps import read_app_actions, read_last_app_action
from kelso.lib.bundle import scan_bundles
from kelso.lib.config import VAR_DIRS, VOLUME_KINDS, load_config_file
from kelso.lib.crypto import FernetCryptoEngine
from kelso.lib.kelso import KelsoCtx
from kelso.lib.util import refuse_root

BASIC = "io.p2net.basic-features"


# --- lifecycle -------------------------------------------------------------


def _ps_row(stdout: str, app_id: str) -> list[str]:
  return next(line for line in stdout.splitlines() if line.startswith(app_id)).split()


def test_start_materializes_compose_and_port_state(kelso_env):
  result = kelso_env.run("start", "ports-demo")

  assert result.returncode == 0, result.stderr
  compose = yaml.safe_load(
    (kelso_env.run_root / "ports-demo" / "compose.yml").read_text()
  )
  assert compose["name"] == "ports-demo"
  assert compose["services"]["main"]["ports"] == ["41000:8080", "9000:80"]

  db = kelso_env.read_db()
  web = db["routes"]["ports-demo"]["web"]
  admin = db["routes"]["ports-demo"]["admin"]
  assert web["host_port"] == 41000
  assert web["scheme"] == "http"
  assert admin["host_port"] == 9000
  assert "publish" not in admin


def test_start_ps_stop_tracks_docker_reality(kelso_env):
  catalog = kelso_env.run("catalog")
  assert catalog.returncode == 0, catalog.stderr
  assert "ports-demo" in catalog.stdout

  not_installed = kelso_env.run("ps")
  assert not_installed.returncode == 0
  assert "ports-demo" not in not_installed.stdout

  started = kelso_env.run("start", "ports-demo")
  assert started.returncode == 0, started.stderr
  assert "Running ports-demo" in started.stdout
  assert "Containers:  main, image=alpine:latest" in started.stdout
  assert "main:8080/tcp <- http://localhost:41000" in started.stdout
  assert "kelso logs -f ports-demo" in started.stdout

  concise = kelso_env.run("ps")
  assert concise.returncode == 0, concise.stderr
  assert concise.stdout.splitlines()[0].split() == [
    "APP_ID",
    "STATUS",
    "CONFIG",
    "VOLUMES",
    "LAST_ACTION",
  ]
  assert _ps_row(concise.stdout, "ports-demo") == [
    "ports-demo",
    "running",
    "ready",
    "0",
    "started",
  ]

  stopped = kelso_env.run("stop", "ports-demo")
  assert stopped.returncode == 0, stopped.stderr

  assert _ps_row(kelso_env.run("ps").stdout, "ports-demo") == [
    "ports-demo",
    "-",
    "ready",
    "0",
    "stopped",
  ]

  calls = [
    json.loads(line)["args"] for line in kelso_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] in calls
  assert ["compose", "down"] in calls


def test_start_receipt_uses_kelso_address_for_local_urls(kelso_env):
  kelso_env.config.write_text(
    kelso_env.config.read_text().replace(
      "port_base = 41000\n",
      'port_base = 41000\nkelso_address = "10.0.0.5"\n',
    )
  )
  started = kelso_env.run("start", "ports-demo")
  assert started.returncode == 0, started.stderr
  assert "http://10.0.0.5:41000" in started.stdout
  assert "http://localhost:41000" not in started.stdout


def test_rm_removes_run_state_configuration_and_managed_volumes(kelso_env):
  started = kelso_env.run("start", BASIC, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr
  assert (kelso_env.run_root / BASIC).is_dir()
  assert (kelso_env.volumes_root / "data" / BASIC / "config").is_dir()
  assert (kelso_env.volumes_root / "temp" / BASIC / "cache").is_dir()

  removed = kelso_env.run("rm", BASIC, "-y")
  assert removed.returncode == 0, removed.stderr

  assert not (kelso_env.run_root / BASIC).exists()
  assert not kelso_env.app_logtab(BASIC).exists()
  assert not (kelso_env.volumes_root / "data" / BASIC).exists()
  assert not (kelso_env.volumes_root / "temp" / BASIC).exists()
  assert BASIC not in kelso_env.read_db().get("routes", {})


def test_rm_leaves_the_catalog_entry_alone(kelso_env):
  app_id = "ports-demo"
  bundle = kelso_env.main_repo / f"{app_id}.klso"
  assert kelso_env.run("start", app_id).returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  assert kelso_env.run("rm", app_id, "-y").returncode == 0
  assert not (kelso_env.run_root / app_id).exists()
  assert bundle.is_dir()


def test_inspect_shows_live_state_for_an_installed_app(kelso_env):
  assert kelso_env.run("start", "ports-demo").returncode == 0
  inspected = kelso_env.run("inspect", "ports-demo")
  assert inspected.returncode == 0, inspected.stderr
  assert "running" in inspected.stdout
  assert "Containers:  main, image=alpine:latest" in inspected.stdout
  assert "main:8080/tcp <- http://localhost:41000" in inspected.stdout
  assert "kelso logs -f ports-demo" in inspected.stdout
  assert "Last action:" in inspected.stdout
  assert "subdomain: ports" in inspected.stdout
  assert "Note:" not in inspected.stdout


def test_catalog_shows_available_apps_ps_hides_until_installed(kelso_env):
  app_id = "ports-demo"
  catalog = kelso_env.run("catalog")
  assert any(line.startswith(app_id) for line in catalog.stdout.splitlines())

  ps = kelso_env.run("ps")
  assert app_id not in ps.stdout

  # Unstaged apps have no run/ copy; inspect a path instead of the catalog id.
  bundle = kelso_env.main_repo / f"{app_id}.klso"
  inspected = kelso_env.run("inspect", str(bundle))
  assert inspected.returncode == 0, inspected.stderr


def test_inspect_shows_config_status(kelso_env):
  assert kelso_env.run("install", BASIC).returncode == 0

  before = kelso_env.run("inspect", BASIC)
  assert before.returncode == 0, before.stderr
  assert "admin_user: (required)" in before.stdout
  assert "admin_pass: (secret)" in before.stdout

  assert kelso_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0
  after = kelso_env.run("inspect", BASIC)
  assert after.returncode == 0, after.stderr
  assert "admin_user: alice" in after.stdout
  assert "admin_pass: (secret)" in after.stdout


def test_inspect_notes_when_the_source_manifest_has_drifted(kelso_env):
  assert kelso_env.run("install", BASIC).returncode == 0
  source = kelso_env.main_repo / f"{BASIC}.klso" / "manifest.toml"
  source.write_text(source.read_text() + "\n# edited after staging\n")

  inspected = kelso_env.run("inspect", BASIC)
  assert inspected.returncode == 0, inspected.stderr
  assert "Note:" in inspected.stdout
  assert (
    f"manifest has changed, `kelso install {BASIC}` may be required to "
    f"reflect changes" in inspected.stdout
  )


def test_inspect_by_path_shows_declared_config_without_installing(kelso_env):
  bundle = kelso_env.main_repo / f"{BASIC}.klso"
  inspected = kelso_env.run("inspect", str(bundle))
  assert inspected.returncode == 0, inspected.stderr
  assert "admin_user: (required)" in inspected.stdout
  assert "admin_pass: (secret)" in inspected.stdout
  assert not kelso_env.app_logtab(BASIC).exists()
  assert "Note:" not in inspected.stdout
  assert "Last action:" not in inspected.stdout
  assert "State:" not in inspected.stdout


def test_logs_accepts_native_flags_before_app(kelso_env):
  assert kelso_env.run("start", "ports-demo").returncode == 0
  # Fake docker ignores unknown compose args; success means argparse accepted order.
  result = kelso_env.run("logs", "-f", "--tail", "10", "ports-demo")
  assert result.returncode == 0, result.stderr
  calls = [
    json.loads(line)["args"] for line in kelso_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "logs", "--follow", "--tail", "10"] in calls


# --- commands --------------------------------------------------------------


def test_cmd_lists_and_runs_manifest_commands(kelso_env):
  app = kelso_env.main_repo / "cmd-demo.klso"
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
desc = "Print pong"

[commands.argv]
cmd = ["echo", "hello"]
desc = "List-form command"
"""
  )

  assert kelso_env.run("start", "cmd-demo").returncode == 0

  listed = kelso_env.run("cmd", "cmd-demo")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.splitlines()[0].split() == [
    "COMMAND",
    "DESCRIPTION",
    "RUN_UNIT",
  ]
  assert "ping" in listed.stdout
  assert "Print pong" in listed.stdout
  assert "argv" in listed.stdout

  ran = kelso_env.run("cmd", "cmd-demo", "ping", "extra")
  assert ran.returncode == 0, ran.stderr
  calls = [
    json.loads(line)["args"] for line in kelso_env.docker_log.read_text().splitlines()
  ]
  assert [
    "compose",
    "exec",
    "main",
    "/bin/sh",
    "-c",
    'echo pong "$@"',
    "_",
    "extra",
  ] in calls

  list_form = kelso_env.run("cmd", "cmd-demo", "argv", "world")
  assert list_form.returncode == 0, list_form.stderr
  calls = [
    json.loads(line)["args"] for line in kelso_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "exec", "main", "echo", "hello", "world"] in calls


def test_cmd_uses_run_when_container_is_stopped(kelso_env):
  app = kelso_env.main_repo / "cmd-demo.klso"
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

  not_staged = kelso_env.run("cmd", "cmd-demo")
  assert not_staged.returncode == 1
  assert "not installed" in not_staged.stderr

  assert kelso_env.run("install", "cmd-demo").returncode == 0
  one_off = kelso_env.run("cmd", "cmd-demo", "ping", "extra")
  assert one_off.returncode == 0, one_off.stderr
  calls = [
    json.loads(line)["args"] for line in kelso_env.docker_log.read_text().splitlines()
  ]
  assert [
    "compose",
    "run",
    "--rm",
    "--no-deps",
    "main",
    "/bin/sh",
    "-c",
    'echo pong "$@"',
    "_",
    "extra",
  ] in calls

  assert kelso_env.run("start", "cmd-demo").returncode == 0
  missing = kelso_env.run("cmd", "cmd-demo", "nope")
  assert missing.returncode == 1
  assert "Unknown command 'nope'" in missing.stderr
  assert "kelso cmd cmd-demo" in missing.stderr


# --- refusals --------------------------------------------------------------


def test_invalid_manifest_is_rejected_before_start(kelso_env):
  app = kelso_env.main_repo / "invalid-mount.klso"
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

  result = kelso_env.run("start", "invalid-mount")

  assert result.returncode == 1
  assert "volume 'missing' is not declared" in result.stderr
  assert not (kelso_env.run_root / "invalid-mount").exists()


def test_start_unknown_id_errors(kelso_env):
  result = kelso_env.run("start", "nope")
  assert result.returncode == 1
  assert "No app found" in result.stderr


def test_start_invalid_path_arg_errors(kelso_env):
  not_bundle = kelso_env.root / "not-a-bundle"
  not_bundle.mkdir()
  bad_suffix = kelso_env.run("start", str(not_bundle))
  assert bad_suffix.returncode == 1
  assert "must end in .klso" in bad_suffix.stderr

  no_manifest = kelso_env.root / "empty.klso"
  no_manifest.mkdir()
  missing = kelso_env.run("start", str(no_manifest))
  assert missing.returncode == 1
  assert "missing manifest.toml" in missing.stderr

  absent = kelso_env.run("start", "./nope.klso")
  assert absent.returncode == 1
  assert "not a directory" in absent.stderr

  assert not (kelso_env.run_root / "empty").exists()


def test_start_by_path_from_arbitrary_dir(kelso_env):
  app_id = "ports-demo"
  elsewhere = kelso_env.root / "elsewhere" / f"{app_id}.klso"
  shutil.copytree(kelso_env.main_repo / f"{app_id}.klso", elsewhere)
  shutil.rmtree(kelso_env.main_repo / f"{app_id}.klso")

  started = kelso_env.run("start", str(elsewhere))
  assert started.returncode == 0, started.stderr
  # A bundle named by path belongs to no repo, so nothing is added to one.
  assert not (kelso_env.main_repo / f"{app_id}.klso").exists()
  assert kelso_env.app_logtab(app_id).read_text().count(str(elsewhere.resolve()))

  stopped = kelso_env.run("stop", app_id)
  assert stopped.returncode == 0, stopped.stderr
  assert kelso_env.run("start", str(elsewhere)).returncode == 0


def test_start_from_a_conflicting_path_is_refused(kelso_env):
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  other = kelso_env.root / "elsewhere" / f"{app_id}.klso"
  shutil.copytree(kelso_env.main_repo / f"{app_id}.klso", other)

  result = kelso_env.run("start", str(other))
  assert result.returncode == 1
  assert "previously installed from" in result.stderr


def test_missing_run_directory_with_container_refuses_lifecycle(kelso_env):
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  shutil.rmtree(kelso_env.run_root / app_id)

  doctor = kelso_env.run("doctor")
  assert doctor.returncode == 1
  assert "run directory missing" in doctor.stderr
  assert "manual container recovery required" in doctor.stderr

  for command in (("stop",), ("rm", "-y")):
    refused = kelso_env.run(*command, app_id)
    assert refused.returncode == 1
    assert "fake-container" in refused.stderr

  # Port claims survive a refused rm; nothing is purged.
  assert app_id in kelso_env.read_db().get("routes", {})
  assert kelso_env.docker_state.exists()


def test_removed_app_bundle_remains_runnable_from_the_staged_copy(kelso_env):
  """The run copy is what kelso runs, so deleting apps/<id>.klso is survivable.

  It still shows up as a problem in `doctor` -- nothing can re-stage the app
  until the catalog entry is back -- but stop and start keep working.
  """
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  shutil.rmtree(kelso_env.main_repo / f"{app_id}.klso")

  doctor = kelso_env.run("doctor")
  assert doctor.returncode == 1
  assert "app bundle missing" in doctor.stderr

  stopped = kelso_env.run("stop", app_id)
  assert stopped.returncode == 0, stopped.stderr

  restarted = kelso_env.run("start", app_id)
  assert restarted.returncode == 0, restarted.stderr

  restaged = kelso_env.run("install", app_id)
  assert restaged.returncode == 1
  assert "No app found" in restaged.stderr


def test_reload_picks_up_a_changed_manifest_and_comes_back_up(kelso_env):
  """The whole point: edit the bundle, reload, and the running app has it."""
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  manifest = kelso_env.main_repo / f"{app_id}.klso" / "manifest.toml"
  manifest.write_text(manifest.read_text().replace("0.1.0", "0.2.0"))

  reloaded = kelso_env.run("reload", app_id)
  assert reloaded.returncode == 0, reloaded.stderr
  assert f"Reloaded {app_id}" in reloaded.stdout

  staged = (kelso_env.run_root / app_id / "bundle" / "manifest.toml").read_text()
  assert "0.2.0" in staged
  assert _ps_row(kelso_env.run("ps").stdout, app_id)[1] == "running"


def test_reload_of_a_stopped_app_does_not_start_it(kelso_env):
  """A reload is never a way to start something: it puts back what it found."""
  app_id = "ports-demo"
  assert kelso_env.run("install", app_id).returncode == 0

  reloaded = kelso_env.run("reload", app_id)
  assert reloaded.returncode == 0, reloaded.stderr
  assert f"Re-installed {app_id}" in reloaded.stdout
  assert "it was not running" in reloaded.stdout
  # Installed but never started reads as "-", not "stopped".
  assert _ps_row(kelso_env.run("ps").stdout, app_id)[1] == "-"


def test_missing_config_is_an_actionable_error(kelso_env, monkeypatch, tmp_path):
  monkeypatch.setenv("KELSO_CONFIG", str(tmp_path / "missing.toml"))

  result = kelso_env.run("ps")

  assert result.returncode == 1
  assert "Error: KELSO_CONFIG is set" in result.stderr
  assert "Traceback" not in result.stderr


# --- config ----------------------------------------------------------------


def test_config_before_staging_reads_the_bundle(kelso_env):
  """Values can be set before the first stage; the source is the only manifest
  there is, and `stage` keeps whatever is already on file."""
  listed = kelso_env.run("config", BASIC)
  assert listed.returncode == 0, listed.stderr
  assert "admin_user" in listed.stdout

  early = kelso_env.run("config", BASIC, "--set", "admin_user=alice")
  assert early.returncode == 0, early.stderr

  assert kelso_env.run("install", BASIC).returncode == 0
  kept = kelso_env.run("config", BASIC, "--get", "admin_user")
  assert kept.stdout.strip() == "alice"


def test_binding_before_staging_applies_at_the_first_start(kelso_env):
  """The bind is recorded against the source's manifest, so the very first
  stage already has it -- no start-then-bind-then-restage round trip."""
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()

  bound = kelso_env.run("config", app_id, "--bind", "hostvol1=media")
  assert bound.returncode == 0, bound.stderr

  assert kelso_env.run("start", app_id).returncode == 0
  link = kelso_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.resolve() == host_path


def test_config_of_a_staged_app_reads_the_run_copy(kelso_env):
  """Not the source, which may have moved on since: the run copy is what the
  app will actually start with."""
  assert kelso_env.run("install", BASIC).returncode == 0

  manifest = kelso_env.main_repo / f"{BASIC}.klso" / "manifest.toml"
  manifest.write_text(
    manifest.read_text().replace("[volumes]", "since_staging = {}\n\n[volumes]")
  )
  # The source now declares it; the installed app does not.
  assert "since_staging" in kelso_env.run("inspect", str(manifest.parent)).stdout
  assert "since_staging" not in kelso_env.run("config", BASIC).stdout

  refused = kelso_env.run("config", BASIC, "--set", "since_staging=x")
  assert refused.returncode == 1
  assert "No config since_staging" in refused.stderr


def test_config_refuses_an_app_it_has_no_manifest_for(kelso_env):
  gone = kelso_env.run("config", "no-such-app")
  assert gone.returncode == 1
  assert "No app found" in gone.stderr


def test_assigning_a_route_before_install_is_recorded(kelso_env):
  """The provider is contacted by `start`, so an assignment can be made first."""
  assigned = kelso_env.run("config", "routes-demo", "--route", "main=web")
  assert assigned.returncode == 0, assigned.stderr
  assert "applied on next start" in assigned.stdout

  ctx = KelsoCtx(load_config_file(kelso_env.config))
  assert ctx.app_store("routes-demo").get_route_assignment("main") == "web"


def test_config_set_secret(kelso_env):
  assert kelso_env.run("install", BASIC).returncode == 0
  assert kelso_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0

  missing = kelso_env.run("config", BASIC, "--set", "admin_user")
  assert missing.returncode == 1
  assert "KEY=VALUE" in missing.stderr

  password = "hunter2"
  stored = kelso_env.run("config", BASIC, "--set", f"admin_pass={password}")
  assert stored.returncode == 0, stored.stderr

  got = kelso_env.run("config", BASIC, "--get", "admin_pass")
  assert got.returncode == 0, got.stderr
  assert got.stdout.strip() == "set"

  revealed = kelso_env.run("config", BASIC, "--get", "admin_pass", "--show-secret")
  assert revealed.returncode == 0, revealed.stderr
  assert revealed.stdout.strip() == password

  listed = kelso_env.run("config", BASIC)
  assert listed.returncode == 0, listed.stderr
  assert password not in listed.stdout
  assert "(secret)" in listed.stdout


def test_config_set_while_running_warns(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  result = kelso_env.run("config", BASIC, "--set", "admin_user=bob")
  assert result.returncode == 0, result.stderr
  assert "is running" in result.stderr
  assert f"kelso stop {BASIC}" in result.stderr
  assert f"kelso start {BASIC}" in result.stderr


def test_config_set_subdomain_overrides_the_manifest(kelso_env):
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  assert kelso_env.read_db()["routes"][app_id]["web"]["subdomain"] == "web-ports"

  running = kelso_env.run("config", app_id, "--set", "subdomain=lab")
  assert running.returncode == 1
  assert f"kelso stop {app_id}" in running.stderr
  assert kelso_env.read_db()["routes"][app_id]["web"]["subdomain"] == "web-ports"

  assert kelso_env.run("stop", app_id).returncode == 0
  set_result = kelso_env.run("config", app_id, "--set", "subdomain=lab")
  assert set_result.returncode == 0, set_result.stderr

  got = kelso_env.run("config", app_id, "--get", "subdomain")
  assert got.stdout.strip() == "lab"
  assert kelso_env.read_db()["routes"][app_id]["web"]["subdomain"] == "web-lab"
  assert kelso_env.read_db()["routes"][app_id]["admin"]["subdomain"] == "admin-lab"

  listed = kelso_env.run("config", app_id)
  assert listed.returncode == 0, listed.stderr
  assert "subdomain" in listed.stdout
  assert "lab" in listed.stdout


def test_config_set_subdomain_rejects_a_dotted_name(kelso_env):
  assert kelso_env.run("install", "ports-demo").returncode == 0
  result = kelso_env.run("config", "ports-demo", "--set", "subdomain=foo.bar")
  assert result.returncode == 1
  assert "foo.bar" in result.stderr
  assert "identifier" in result.stderr


def test_system_config_is_encrypted_listed_and_unset(kelso_env):
  route_key = "route_provider.nginx_proxy_manager.password"
  other_key = "backup.api_key"
  route_password = "correct horse battery staple"

  stored = kelso_env.run(
    "config-sys", "--stdin", route_key, input=f"{route_password}\n"
  )
  assert stored.returncode == 0, stored.stderr
  assert (
    kelso_env.run("config-sys", "--set", f"{other_key}=backup-secret").returncode == 0
  )

  db = kelso_env.read_db()
  encrypted = db["system"]["secrets"][route_key]
  assert encrypted != route_password
  assert route_password not in encrypted

  listed = kelso_env.run("config-sys")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.splitlines() == [other_key, route_key]
  assert route_password not in listed.stdout
  assert "backup-secret" not in listed.stdout

  unset = kelso_env.run("config-sys", "--unset", route_key)
  assert unset.returncode == 0, unset.stderr
  assert kelso_env.run("config-sys").stdout.splitlines() == [other_key]

  old_command = kelso_env.run("provider", "set-password")
  assert old_command.returncode == 2
  assert "invalid choice" in old_command.stderr


def test_decrypt_round_trips_a_stored_secret(kelso_env):
  secret = "correct horse battery staple"
  key = "backup.api_key"
  assert (
    kelso_env.run("config-sys", "--stdin", key, input=f"{secret}\n").returncode == 0
  )

  blob = kelso_env.read_db()["system"]["secrets"][key]
  assert secret not in blob

  result = kelso_env.run("decrypt", input=f"{blob}\n")
  assert result.returncode == 0, result.stderr
  assert result.stdout.strip() == secret


def test_decrypt_tolerates_surrounding_whitespace(kelso_env):
  # A blob pasted out of the logtab or a shell pipeline arrives padded.
  kelso_env.run("config-sys", "--set", "k=hunter2")
  blob = kelso_env.read_db()["system"]["secrets"]["k"]

  result = kelso_env.run("decrypt", input=f"   {blob}  \n")
  assert result.returncode == 0, result.stderr
  assert result.stdout.strip() == "hunter2"


@pytest.mark.parametrize(
  "blob",
  [
    "not-a-fernet-token",
    # Well-formed Fernet minted under a different master key.
    FernetCryptoEngine("some other key").encrypt("hunter2"),
  ],
)
def test_decrypt_refuses_what_it_cannot_authenticate(kelso_env, blob):
  result = kelso_env.run("decrypt", input=f"{blob}\n")

  assert result.returncode == 1
  assert "Could not decrypt" in result.stderr
  # Nothing plausible-looking leaks onto stdout on failure.
  assert result.stdout.strip() == ""


def test_decrypt_refuses_empty_stdin(kelso_env):
  result = kelso_env.run("decrypt", input="\n")
  assert result.returncode == 1
  assert "Nothing on stdin" in result.stderr


def test_decrypt_refuses_when_there_is_no_master_key(kelso_env):
  # Without a key the noop engine would echo the input back and call it success.
  (kelso_env.root / "master.key").write_text("")
  blob = FernetCryptoEngine("0" * 64).encrypt("hunter2")

  result = kelso_env.run("decrypt", input=f"{blob}\n")
  assert result.returncode == 1
  assert "No master key" in result.stderr
  assert "hunter2" not in result.stdout


def test_host_bind_one_shot_via_start(kelso_env):
  app_id = "host-volumes"
  blocked = kelso_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "Bind with `kelso config host-volumes --bind hostvol1=" in blocked.stderr

  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  started = kelso_env.run("start", app_id, "--bind", "hostvol1=media")
  assert started.returncode == 0, started.stderr
  link = kelso_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.is_symlink()
  assert link.resolve() == host_path


def test_host_links_belong_to_the_run_not_the_stage(kelso_env):
  """`bind` records; `start` links; `stop` unlinks.

  A bind that only ever reached the config store was the original bug: compose
  mounts `volumes/host/<name>` regardless, so docker created it as an empty
  directory and the app came up against that instead of the host path.
  """
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  host_root = kelso_env.run_root / app_id / "volumes" / "host"
  link = host_root / "hostvol1"

  assert kelso_env.run("install", app_id).returncode == 0
  assert not host_root.exists(), "staging has no business linking somebody's data"

  bound = kelso_env.run("config", app_id, "--bind", "hostvol1=media")
  assert bound.returncode == 0, bound.stderr
  assert not host_root.exists()

  started = kelso_env.run("start", app_id)
  assert started.returncode == 0, started.stderr
  assert link.is_symlink()
  assert link.resolve() == host_path

  assert kelso_env.run("stop", app_id).returncode == 0
  assert not host_root.exists()
  assert host_path.is_dir(), "unlinking must not touch what was linked to"


def test_restaging_leaves_the_binds_alone(kelso_env):
  """Staging rebuilds the run dir, but a bind survives it and still applies."""
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  assert kelso_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  assert kelso_env.run("install", app_id).returncode == 0
  assert kelso_env.run("start", app_id).returncode == 0
  link = kelso_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.resolve() == host_path


def test_rebinding_takes_effect_at_the_next_start(kelso_env):
  """A second bind moves the link; the old target is left alone."""
  app_id = "host-volumes"
  first = kelso_env.root / "external-data"
  second = kelso_env.root / "other-data"
  first.mkdir()
  second.mkdir()
  assert kelso_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0

  link = kelso_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  rebound = kelso_env.run("config", app_id, "--bind", "hostvol1=other")
  assert rebound.returncode == 0, rebound.stderr
  assert "is running" in rebound.stderr
  assert link.resolve() == first, "a running app's links must not move"

  assert kelso_env.run("stop", app_id).returncode == 0
  assert kelso_env.run("start", app_id).returncode == 0
  assert link.resolve() == second
  assert first.is_dir()


def test_start_replaces_whatever_is_in_the_host_directory(kelso_env):
  """`host/` is rebuilt from the binds, so nothing stale can survive a start.

  Both cases at once: the empty directory docker leaves when it mounts a bind
  source that was not linked, and a link pointing somewhere the bind no longer
  says.
  """
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  assert kelso_env.run("install", app_id).returncode == 0
  assert kelso_env.run("config", app_id, "--bind", "hostvol1=media").returncode == 0

  link = kelso_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  link.mkdir(parents=True)  # what docker left behind

  assert kelso_env.run("start", app_id).returncode == 0
  assert link.is_symlink()
  assert link.resolve() == host_path

  assert kelso_env.run("stop", app_id).returncode == 0
  link.parent.mkdir(parents=True, exist_ok=True)
  link.symlink_to(kelso_env.root)  # a link from some earlier bind

  assert kelso_env.run("start", app_id).returncode == 0
  assert link.resolve() == host_path


def test_start_refuses_when_a_bound_path_has_gone(kelso_env):
  """A bind whose host path is no longer there must not start the app.

  `bind` checks the path at bind time, so the way to reach this is a share
  that stopped being mounted -- exactly when starting anyway is worst, since
  docker would recreate the path as an empty directory and the app would come
  up against an empty volume instead of failing.
  """
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  assert kelso_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_path)

  blocked = kelso_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "path does not exist" in blocked.stderr
  assert str(host_path) in blocked.stderr
  assert not host_path.exists(), "a failed start must not recreate the host path"

  # The bind is still on file, so putting the path back is the whole fix.
  host_path.mkdir()
  assert kelso_env.run("start", app_id).returncode == 0


def test_missing_host_volume_path_blocks_stage(kelso_env):
  """A bound host volume whose path is gone must refuse restaging."""
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  assert kelso_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_path)
  blocked = kelso_env.run("install", app_id)
  assert blocked.returncode == 1
  assert "path does not exist" in blocked.stderr
  assert str(host_path) in blocked.stderr


def test_host_volume_may_be_a_file(kelso_env):
  """A host volume can point at a single file, not only a directory."""
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.write_text("just a file")

  started = kelso_env.run("start", app_id, "--bind", "hostvol1=media")
  assert started.returncode == 0, started.stderr
  link = kelso_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.is_symlink()
  assert link.resolve() == host_path
  assert link.resolve().is_file()


def test_require_mount_refuses_unmounted_path(kelso_env):
  """require_mount catches an empty mount-point directory."""
  with open(kelso_env.config, "a") as f:
    f.write('\n[host_volume.nfs]\npath = "external-data"\nrequire_mount = true\n')
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()

  assert kelso_env.run("install", "host-volumes").returncode == 0
  refused = kelso_env.run("config", "host-volumes", "--bind", "hostvol1=nfs")
  assert refused.returncode == 1
  assert "not mounted" in refused.stderr


def test_require_mount_blocks_stage_when_unmounted(kelso_env):
  """A previously bound require_mount volume blocks restaging if unmounted."""
  # Bind against a real mount first (/), then retarget the tag at an ordinary
  # directory so restaging sees require_mount fail through ConfigIssue.
  with open(kelso_env.config, "a") as f:
    f.write('\n[host_volume.rootfs]\npath = "/"\nrequire_mount = true\n')

  assert kelso_env.run("install", "host-volumes").returncode == 0
  assert (
    kelso_env.run("config", "host-volumes", "--bind", "hostvol1=rootfs").returncode == 0
  )

  # Rewrite the same tag to a non-mount path without clearing the bind.
  config_text = kelso_env.config.read_text()
  kelso_env.config.write_text(
    config_text.replace('path = "/"', 'path = "external-data"')
  )
  (kelso_env.root / "external-data").mkdir(exist_ok=True)

  blocked = kelso_env.run("install", "host-volumes")
  assert blocked.returncode == 1
  assert "not mounted" in blocked.stderr


def test_readonly_host_volume_refuses_writable_app_volume(kelso_env):
  """A readonly host volume may only be bound by a readonly app volume."""
  with open(kelso_env.config, "a") as f:
    f.write('\n[host_volume.ro_media]\npath = "ro-data"\nreadonly = true\n')
  (kelso_env.root / "ro-data").mkdir()

  assert kelso_env.run("install", "host-volumes").returncode == 0
  refused = kelso_env.run("config", "host-volumes", "--bind", "hostvol1=ro_media")
  assert refused.returncode == 1
  assert "readonly" in refused.stderr


# --- start blockers --------------------------------------------------------


def test_missing_secret_blocks_start_with_recovery_command(kelso_env):
  app_id = "needs-secret"
  app = kelso_env.main_repo / f"{app_id}.klso"
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

  blocked = kelso_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "Set with `kelso config`" in blocked.stderr
  assert (kelso_env.run_root / app_id).is_dir()

  needs_config_row = _ps_row(kelso_env.run("ps").stdout, app_id)
  assert needs_config_row[:4] == [app_id, "-", "missing", "0"]

  configured = kelso_env.run("config", app_id, "--set", "api_key=sekrit")
  assert configured.returncode == 0, configured.stderr
  started = kelso_env.run("start", app_id)
  assert started.returncode == 0, started.stderr


def test_a_required_non_secret_value_blocks_start_and_shows_as_missing_config(
  kelso_env,
):
  """The non-secret counterpart of the missing-secret case.

  A value with no default must be supplied whether or not it is secret; this
  was once silently treated as satisfied, so an app could start with it empty.
  A dedicated bundle whose only config is non-secret and default-less, so the
  blocker cannot be attributed to some other value.
  """
  app_id = "needs-value"
  app = kelso_env.main_repo / f"{app_id}.klso"
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

  blocked = kelso_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "hostname is unset and no default specified" in blocked.stderr
  assert "Set with `kelso config`" in blocked.stderr
  # stage() materializes before start_blockers are judged, so the run dir
  # exists; what must not have happened is the container starting.
  calls = [
    json.loads(line)["args"] for line in kelso_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] not in calls

  row = _ps_row(kelso_env.run("ps").stdout, app_id)
  assert row[:4] == [app_id, "-", "missing", "0"]

  assert kelso_env.run("config", app_id, "--set", "hostname=box").returncode == 0
  assert kelso_env.run("start", app_id).returncode == 0


# --- ps status accuracy ----------------------------------------------------
#
# `start` establishes preconditions before it judges readiness: stage() generates
# config defaults and reallocates every route, *then* evaluates blockers. `ps`
# calls load_run_data() with neither having run, so anything start repairs itself
# must not be reported as something the operator has to fix.


def test_ps_reports_config_readiness_and_volume_count(kelso_env):
  assert kelso_env.run("install", BASIC).returncode == 0
  row = _ps_row(kelso_env.run("ps").stdout, BASIC)
  assert row[1:4] == ["-", "missing", "3"]

  assert kelso_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0
  row = _ps_row(kelso_env.run("ps").stdout, BASIC)
  assert row[1:4] == ["-", "ready", "3"]


def test_unallocated_routes_are_not_reported_as_missing_config(kelso_env):
  """Unallocated routes are self-healing; CONFIG must stay ready."""
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  # Route rows gone while the run dir survives -- the pre-start allocation state.
  KelsoCtx(load_config_file(kelso_env.config)).kelso_db.clear_routes(app_id)
  assert (kelso_env.run_root / app_id / "compose.yml").is_file()

  listed = kelso_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert _ps_row(listed.stdout, app_id)[2] == "ready"

  # ...and it is genuinely startable, configuring nothing.
  assert kelso_env.run("start", app_id).returncode == 0


def test_an_unmet_bind_is_reported_as_missing_config(kelso_env):
  """A host volume whose path has gone is the operator's to fix."""
  app_id = "host-volumes"
  host_path = kelso_env.root / "external-data"
  host_path.mkdir()
  assert kelso_env.run("install", app_id).returncode == 0
  assert kelso_env.run("config", app_id, "--bind", "hostvol1=media").returncode == 0
  assert kelso_env.run("start", app_id).returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_path)

  listed = kelso_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert _ps_row(listed.stdout, app_id)[2] == "missing"


def test_an_unloadable_app_is_not_reported_as_missing_config(kelso_env):
  """A staged bundle that will not parse is unknown, not unconfigured."""
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  assert kelso_env.run("stop", app_id).returncode == 0

  (kelso_env.run_root / app_id / "bundle" / "manifest.toml").write_text("not toml {[")

  listed = kelso_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  row = _ps_row(listed.stdout, app_id)
  assert row[1:4] == ["-", "-", "-"]


# --- doctor ----------------------------------------------------------------


def test_doctor_reports_orphaned_routes(kelso_env):
  """Routes are the only per-app state kelsodb still holds.

  Config and binds live in config/<app_id>.logtab, so a kelsodb entry with no
  run directory can only be a route allocation nothing owns -- which still
  pins a host port and so is worth reporting.
  """
  kelso_env.seed_db(
    {
      "routes/io.example.abandoned/web": {
        "name": "web",
        "subdomain": "",
        "run_unit_name": "main",
        "host_port": 41000,
        "container_port": 8080,
        "proto": "tcp",
        "scheme": "http",
      }
    }
  )

  ps = kelso_env.run("ps")
  assert _ps_row(ps.stdout, "io.example.abandoned")[1:4] == ["-", "-", "-"]

  doctor = kelso_env.run("doctor")
  assert doctor.returncode == 1
  assert "orphaned route allocation" in doctor.stderr


def test_doctor_exposes_mixed_container_states(kelso_env):
  app_id = "io.example.docker-only"
  kelso_env.set_containers(
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

  doctor = kelso_env.run("doctor")
  assert doctor.returncode == 1
  assert "mixed container states" in doctor.stderr


# --- route provider --------------------------------------------------------
#
# The provider is stubbed here. Registering against a real Nginx Proxy Manager
# is a live test; what these pin down is *when* kelso calls it.


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
      "kelso.lib.lifecycle.routes.get_route_provider",
      lambda ctx, tag: provider,
    )
    monkeypatch.setattr("kelso.lib.kelso.load_kelso_run_unit_status", lambda: {})
    return provider

  return install


def test_duplicate_fqdn_is_rejected_before_compose_up(
  kelso_env, monkeypatch, stub_provider
):
  # Provider already owns photos.* under another bundle -- start must refuse
  # before compose.
  provider = stub_provider({"photos": "other-routes", "api-photos": "other-routes"})
  docker_calls = []
  monkeypatch.setattr(
    "kelso.lib.lifecycle.run.docker_run_command",
    lambda args, **kwargs: docker_calls.append(args) or "",
  )
  ctx = KelsoCtx(load_config_file(kelso_env.config))

  app = ctx.resolve_app("routes-demo")
  lifecycle.stage(app, ctx.bundle_path(app), ctx)

  start_ctx = KelsoCtx(load_config_file(kelso_env.config))
  with pytest.raises(ValueError, match="already owned"):
    lifecycle.start(app, start_ctx.bundle_path(app), start_ctx)
  assert provider.registered == []
  assert ["compose", "up", "-d"] not in docker_calls


def test_stop_uses_staged_manifest_when_bundle_is_missing(
  kelso_env, monkeypatch, stub_provider
):
  provider = stub_provider()
  monkeypatch.setattr(
    "kelso.lib.lifecycle.run.docker_run_command",
    lambda args, **kwargs: "",
  )
  stage_ctx = KelsoCtx(load_config_file(kelso_env.config))
  app = stage_ctx.resolve_app("routes-demo")
  lifecycle.stage(app, stage_ctx.bundle_path(app), stage_ctx)
  start_ctx = KelsoCtx(load_config_file(kelso_env.config))
  lifecycle.start(app, start_ctx.bundle_path(app), start_ctx)
  shutil.rmtree(kelso_env.main_repo / "routes-demo.klso")

  fresh_ctx = KelsoCtx(load_config_file(kelso_env.config))
  lifecycle.stop("routes-demo", fresh_ctx)
  assert provider.unregistered == [
    ("photos", "kelso.localhost"),
    ("api-photos", "kelso.localhost"),
  ]


# --- bootstrap -------------------------------------------------------------


def test_init_configures_the_default_repos(kelso_env, tmp_path):
  """A fresh root is not an empty store: the catalog has somewhere to come from."""
  root = tmp_path / "fresh"
  result = kelso_env.run("--root", str(root), "init", "--no-mirror", input="\n")
  assert result.returncode == 0, result.stderr

  config = load_config_file(root / "config.toml")

  assert set(config.repos) == {"main", "staples", "demos"}
  assert config.repos["staples"].remote.url == "github://nepthar/kelso/main/apps"
  assert config.repos["demos"].remote.url == "github://nepthar/kelso/main/demo-apps"
  # main is the hand-drop directory and is never mirrored.
  assert not config.repos["main"].mirrored
  # --no-mirror leaves them configured but unfetched, and says so.
  assert "Skipped mirroring" in result.stdout
  assert "kelso repo update" in result.stdout


def test_init_bootstraps_a_usable_root(kelso_env, tmp_path):
  """`kelso init` runs before any config or lock exists.

  Nothing else in the suite calls it, because every other test starts from a
  root the fixture builds by hand.
  """
  root = tmp_path / "fresh"
  # init prompts for the root; an empty line accepts the --root default.
  # --no-mirror because the default repos are on GitHub and the suite does not
  # touch the network; `test_init_configures_the_default_repos` covers the
  # tables it writes.
  result = kelso_env.run("--root", str(root), "init", "--no-mirror", input="\n")

  assert result.returncode == 0, result.stderr
  assert (root / "config.toml").is_file()
  assert (root / "master.key").is_file()
  assert (root / "repos" / "main").is_dir()
  assert (root / "run").is_dir()
  assert (root / "config").is_dir()
  for kind in VOLUME_KINDS:
    assert (root / "volumes" / kind).is_dir(), kind
  for name in VAR_DIRS:
    assert (root / "var" / name).is_dir(), name

  # The master key must be readable back, not merely present: a command against
  # the new root has to load it through load_config_file.
  after = kelso_env.run("--root", str(root), "ps")
  assert after.returncode == 0, after.stderr


def test_gen_masterkey_appends_to_the_keyfile(kelso_env):
  before = (kelso_env.root / "master.key").read_text()
  result = kelso_env.run("config-sys", "gen-masterkey")

  assert result.returncode == 0, result.stderr
  after = (kelso_env.root / "master.key").read_text()
  assert after.startswith(before) and len(after) > len(before)


def test_every_shipped_bundle_stages(kelso_env):
  """Every bundle this repo ships must parse -- real apps and demos alike.

  This previously pointed at an `examples/` directory that does not exist, so
  it globbed nothing and passed unconditionally.
  """
  root = Path(__file__).parents[1]
  shipped = [
    (app_id, apps_dir / rel_path)
    for apps_dir in (root / "apps", root / "demo-apps")
    for app_id, rel_path in scan_bundles(apps_dir)
  ]
  assert shipped, "no bundles found in apps/ or demo-apps/"

  for app_id, source in shipped:
    dest = kelso_env.main_repo / source.name
    if source.is_dir():
      shutil.copytree(source, dest, dirs_exist_ok=True)
    else:
      shutil.copy2(source, dest)
    fresh = KelsoCtx(load_config_file(kelso_env.config))
    app = fresh.resolve_app(app_id)
    lifecycle.stage(app, fresh.bundle_path(app), fresh)
    assert (kelso_env.run_root / app_id / "compose.yml").is_file(), source.name


def test_readme_quickstart_from_repo_apps(kelso_env):
  """Getting-started path from a checkout: start an apps/ bundle by path, then ps.

  Deliberately a bundle shipped in the repo rather than a tests/fixtures one:
  this is the path a reader follows straight from the README, so it should
  break if the shipped bundles do. `demo-routes` because its `lan_only` route
  keeps the LAN receipt line under test.
  """
  bundle = Path(__file__).parents[1] / "demo-apps" / "demo-routes.klso.md"
  assert bundle.is_file(), bundle

  started = kelso_env.run("start", str(bundle))
  assert started.returncode == 0, started.stderr
  assert "Running demo-routes" in started.stdout
  assert "Routes:" in started.stdout

  ps = kelso_env.run("ps")
  assert ps.returncode == 0, ps.stderr
  assert "demo-routes" in ps.stdout


# --- activity log ----------------------------------------------------------


def test_last_action_is_read_in_one_pass(kelso_env):
  """`ps` reports every app's action from a single read of the shared log."""
  assert kelso_env.run("start", "ports-demo").returncode == 0
  assert kelso_env.run("start", "routes-demo").returncode == 0

  ctx = KelsoCtx(load_config_file(kelso_env.config))
  assert {k: v[1] for k, v in read_app_actions(ctx).items()} == {
    "ports-demo": "started",
    "routes-demo": "started",
  }

  listed = kelso_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.count("started") >= 2


def test_removal_is_recorded_when_an_app_is_removed(kelso_env):
  """The activity log outlives the app, so removal is recorded, not erased."""
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0

  ctx = KelsoCtx(load_config_file(kelso_env.config))
  assert read_last_app_action(app_id, ctx) == "started"

  assert kelso_env.run("rm", app_id, "-y").returncode == 0

  assert not (kelso_env.run_root / app_id).exists()
  assert read_last_app_action(app_id, ctx) == "removed"
  assert f"apps/{app_id}/status" in ctx.activity_log.load()


# --- running as root -------------------------------------------------------


def test_refuse_root_is_quiet_for_an_ordinary_user(monkeypatch):
  monkeypatch.setattr(os, "geteuid", lambda: 1000)
  assert refuse_root("kelso") is None


def test_refuse_root_names_who_and_what_to_do(monkeypatch):
  monkeypatch.setattr(os, "geteuid", lambda: 0)
  with pytest.raises(RuntimeError) as raised:
    refuse_root("the kelso daemon")
  assert "the kelso daemon" in str(raised.value)
  assert "sudo" in str(raised.value)


def test_every_command_refuses_to_run_as_root(kelso_env, monkeypatch):
  """`init` included: it is the command that would create the root-owned tree."""
  monkeypatch.setattr(os, "geteuid", lambda: 0)
  for argv in (["init"], ["ps"], ["snapshot", BASIC]):
    refused = kelso_env.run(*argv)
    assert refused.returncode == 1, argv
    assert "refuses to run as root" in refused.stderr, argv


def test_kelsod_refuses_to_run_as_root(monkeypatch, capsys):
  import sys

  from kelso.daemon.server import main as kelsod_main

  monkeypatch.setattr(os, "geteuid", lambda: 0)
  monkeypatch.setattr(sys, "argv", ["kelsod"])
  with pytest.raises(SystemExit) as exit_info:
    kelsod_main()
  assert exit_info.value.code == 1
  assert "kelsod refuses to run as root" in capsys.readouterr().err


# --- activity --------------------------------------------------------------


def _seed_activity(kelso_env, **kwargs):
  """File one activity run straight through the lib, as kelsod's job runner
  would. CLI flows do not record activity themselves -- only daemon jobs do."""
  from datetime import UTC, datetime, timedelta

  from kelso.lib import activity
  from kelso.lib.apps import AppID

  ctx = KelsoCtx(load_config_file(kelso_env.config))
  started = kwargs.get("started", datetime(2026, 8, 25, 3, 30, tzinfo=UTC))
  app = kwargs.get("app", "ports-demo")
  return activity.record_run(
    ctx,
    kwargs.get("verb", "start"),
    {"app": app or ""},
    app_id=AppID(app) if app else None,
    status=kwargs.get("status", activity.OK),
    started=started,
    finished=started + timedelta(seconds=1),
    output=kwargs.get("output", "up and running"),
  )


def test_activity_reports_nothing_when_empty(kelso_env):
  result = kelso_env.run("activity")
  assert result.returncode == 0
  assert "No recorded activity" in result.stdout


def test_activity_lists_recorded_runs(kelso_env):
  _seed_activity(kelso_env, verb="start", app="ports-demo")
  _seed_activity(kelso_env, verb="stop", app="ports-demo", status="error")

  result = kelso_env.run("activity")
  assert result.returncode == 0
  assert "start ports-demo" in result.stdout
  assert "stop ports-demo" in result.stdout
  # Newest first.
  assert result.stdout.index("stop") < result.stdout.index("start")


def test_activity_show_prints_a_run_file(kelso_env):
  _seed_activity(kelso_env, output="the captured output")
  result = kelso_env.run("activity", "--show")
  assert result.returncode == 0
  assert "the captured output" in result.stdout


def test_activity_filters_by_app_stem(kelso_env):
  _seed_activity(kelso_env, app="ports-demo")
  _seed_activity(kelso_env, app="routes-demo")
  result = kelso_env.run("activity", "ports-demo")
  assert "ports-demo" in result.stdout
  assert "routes-demo" not in result.stdout


def test_uninstall_keeps_data_and_configuration(kelso_env):
  started = kelso_env.run("start", BASIC, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr
  data = kelso_env.volumes_root / "data" / BASIC / "config"
  (data / "app.db").write_text("rows")

  removed = kelso_env.run("uninstall", BASIC, "-y")
  assert removed.returncode == 0, removed.stderr

  # The installation goes...
  assert not (kelso_env.run_root / BASIC).exists()
  # ...and everything that would have to be set up again stays.
  assert (data / "app.db").read_text() == "rows"
  assert "config/admin_user" in kelso_env.app_logtab(BASIC).read_text()


def test_uninstall_and_reset_keep_an_app_route_allocation(kelso_env):
  app_id = "ports-demo"
  assert kelso_env.run("start", app_id).returncode == 0
  allocated = kelso_env.read_db()["routes"][app_id]["web"]["host_port"]

  assert kelso_env.run("reset", app_id, "-y").returncode == 0
  assert kelso_env.read_db()["routes"][app_id]["web"]["host_port"] == allocated

  assert kelso_env.run("uninstall", app_id, "-y").returncode == 0
  assert kelso_env.read_db()["routes"][app_id]["web"]["host_port"] == allocated

  # Only rm gives the address back.
  assert kelso_env.run("rm", app_id, "-y").returncode == 0
  assert app_id not in kelso_env.read_db().get("routes", {})


def test_uninstall_then_stage_restores_the_app(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  data = kelso_env.volumes_root / "data" / BASIC / "config"
  (data / "app.db").write_text("rows")
  assert kelso_env.run("uninstall", BASIC, "-y").returncode == 0

  staged = kelso_env.run("install", BASIC)
  assert staged.returncode == 0, staged.stderr
  assert (kelso_env.run_root / BASIC).is_dir()
  # Reinstalling did not ask for the config again, and left the data alone.
  assert (data / "app.db").read_text() == "rows"
  assert "config/admin_user" in kelso_env.app_logtab(BASIC).read_text()


def test_reset_clears_data_and_stages_the_app_again(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  data = kelso_env.volumes_root / "data" / BASIC / "config"
  temp = kelso_env.volumes_root / "temp" / BASIC / "cache"
  (data / "app.db").write_text("rows")
  (data / "nested").mkdir()
  (data / "nested" / "deep.txt").write_text("deeper")
  (temp / "scratch").write_text("junk")

  reset = kelso_env.run("reset", BASIC, "-y")
  assert reset.returncode == 0, reset.stderr

  # Staging rebuilt the installation and the volume directories with it, so
  # the app is installed again with nothing in its volumes.
  assert (kelso_env.run_root / BASIC).is_dir()
  assert data.is_dir() and list(data.iterdir()) == []
  assert temp.is_dir() and list(temp.iterdir()) == []

  # Configuration and secrets were never touched.
  assert "config/admin_user" in kelso_env.app_logtab(BASIC).read_text()


def test_reset_picks_up_a_changed_app_volume(kelso_env):
  """The reason reset re-stages: `app` volumes ship inside the bundle."""
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  staged = kelso_env.run_root / BASIC / "bundle" / "bin" / "hello.sh"
  assert "changed by the bundle author" not in staged.read_text()

  bundle = kelso_env.main_repo / f"{BASIC}.klso" / "bin" / "hello.sh"
  bundle.write_text("#!/bin/sh\necho changed by the bundle author\n")

  assert kelso_env.run("reset", BASIC, "-y").returncode == 0
  assert "changed by the bundle author" in staged.read_text()


def test_reset_refuses_before_deleting_when_the_bundle_is_gone(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  data = kelso_env.volumes_root / "data" / BASIC / "config"
  (data / "app.db").write_text("rows")
  shutil.rmtree(kelso_env.main_repo / f"{BASIC}.klso")

  failed = kelso_env.run("reset", BASIC, "-y")
  assert failed.returncode != 0
  # Planning resolves the bundle, so the refusal costs nothing.
  assert (data / "app.db").read_text() == "rows"
  assert (kelso_env.run_root / BASIC).is_dir()


def test_reset_leaves_an_app_that_starts_again(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  assert kelso_env.run("reset", BASIC, "-y").returncode == 0

  # No re-stage in between: the point of keeping the volume dirs is that the
  # compose file's bind mounts still resolve.
  started = kelso_env.run("start", BASIC)
  assert started.returncode == 0, started.stderr


def test_reset_and_uninstall_record_what_they_did(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  ctx = KelsoCtx(load_config_file(kelso_env.config))

  assert kelso_env.run("reset", BASIC, "-y").returncode == 0
  assert read_last_app_action(BASIC, ctx) == "reset"

  assert kelso_env.run("uninstall", BASIC, "-y").returncode == 0
  assert read_last_app_action(BASIC, ctx) == "uninstalled"


def test_uninstall_confirmation_says_what_it_keeps(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0

  declined = kelso_env.run("uninstall", BASIC, input="n\n")
  assert declined.returncode == 0, declined.stderr
  assert "Configuration and volume data will be kept" in declined.stdout
  assert f"kelso uninstall --purge {BASIC}" in declined.stdout
  # Nothing irreversible is at stake, so it must not borrow rm's warning.
  assert "take a snapshot first" not in declined.stdout
  # Where kelso keeps an installation is not the operator's problem.
  assert str(kelso_env.run_root) not in declined.stdout
  assert "Nothing removed" in declined.stdout
  assert (kelso_env.run_root / BASIC).is_dir()


def test_ps_reports_what_an_uninstalled_app_kept(kelso_env):
  """`ps` must not contradict what `uninstall` said it was keeping."""
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  assert kelso_env.run("uninstall", BASIC, "-y").returncode == 0

  listed = kelso_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  row = _ps_row(listed.stdout, BASIC)
  # STATUS says where it stands, and CONFIG/VOLUMES say what survived --
  # read from the bundle, since there is no staged manifest any more.
  assert row[1] == "uninstalled"
  assert row[2] == "ready"
  assert row[3:5] == ["3", "kept"]


def test_ps_forgets_an_app_once_it_is_purged(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  assert kelso_env.run("uninstall", "--purge", BASIC, "-y").returncode == 0
  assert BASIC not in kelso_env.run("ps").stdout


def test_uninstall_purge_is_rm(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  purged = kelso_env.run("uninstall", "--purge", BASIC, "-y")
  assert purged.returncode == 0, purged.stderr

  assert not (kelso_env.run_root / BASIC).exists()
  assert not kelso_env.app_logtab(BASIC).exists()
  assert not (kelso_env.volumes_root / "data" / BASIC).exists()


def test_uninstall_points_at_purge(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  done = kelso_env.run("uninstall", BASIC, "-y")
  assert "Configuration and volume data were kept" in done.stdout
  assert f"kelso uninstall --purge {BASIC}" in done.stdout


def test_inspect_falls_back_to_the_bundle_when_uninstalled(kelso_env):
  assert kelso_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  assert kelso_env.run("uninstall", BASIC, "-y").returncode == 0

  inspected = kelso_env.run("inspect", BASIC)
  assert inspected.returncode == 0, inspected.stderr
  # Not an errno about a missing manifest.
  assert "No such file" not in inspected.stderr
  assert f"{BASIC} is not installed" in inspected.stdout
  assert f"kelso install {BASIC}" in inspected.stdout
