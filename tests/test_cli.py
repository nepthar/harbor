"""The harbor command surface, driven through `cli.main.run` in-process.

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

from harbor.lib import lifecycle
from harbor.lib.apps import read_app_actions, read_last_app_action
from harbor.lib.config import VAR_DIRS, VOLUME_KINDS, load_config_file
from harbor.lib.crypto import FernetCryptoEngine
from harbor.lib.happ import scan_happs
from harbor.lib.harbor import HarborCtx
from harbor.lib.util import refuse_root

BASIC = "io.p2net.basic-features"


# --- lifecycle -------------------------------------------------------------


def _ps_row(stdout: str, app_id: str) -> list[str]:
  return next(line for line in stdout.splitlines() if line.startswith(app_id)).split()


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
  assert web["scheme"] == "http"
  assert admin["host_port"] == 9000
  assert "publish" not in admin


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
  assert "Containers:  main, image=alpine:latest" in started.stdout
  assert "main:8080/tcp <- http://localhost:41000" in started.stdout
  assert "harbor logs -f ports-demo" in started.stdout

  concise = harbor_env.run("ps")
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

  stopped = harbor_env.run("stop", "ports-demo")
  assert stopped.returncode == 0, stopped.stderr

  assert _ps_row(harbor_env.run("ps").stdout, "ports-demo") == [
    "ports-demo",
    "-",
    "ready",
    "0",
    "stopped",
  ]

  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "up", "-d"] in calls
  assert ["compose", "down"] in calls


def test_start_receipt_uses_harbor_address_for_local_urls(harbor_env):
  harbor_env.config.write_text(
    harbor_env.config.read_text().replace(
      "port_base = 41000\n",
      'port_base = 41000\nharbor_address = "10.0.0.5"\n',
    )
  )
  started = harbor_env.run("start", "ports-demo")
  assert started.returncode == 0, started.stderr
  assert "http://10.0.0.5:41000" in started.stdout
  assert "http://localhost:41000" not in started.stdout


def test_rm_removes_run_state_configuration_and_managed_volumes(harbor_env):
  started = harbor_env.run("start", BASIC, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr
  assert (harbor_env.run_root / BASIC).is_dir()
  assert (harbor_env.volumes_root / "data" / BASIC / "config").is_dir()
  assert (harbor_env.volumes_root / "temp" / BASIC / "cache").is_dir()

  removed = harbor_env.run("rm", BASIC, "-y")
  assert removed.returncode == 0, removed.stderr

  assert not (harbor_env.run_root / BASIC).exists()
  assert not harbor_env.app_logtab(BASIC).exists()
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


def test_inspect_shows_live_state_for_an_installed_app(harbor_env):
  assert harbor_env.run("start", "ports-demo").returncode == 0
  inspected = harbor_env.run("inspect", "ports-demo")
  assert inspected.returncode == 0, inspected.stderr
  assert "running" in inspected.stdout
  assert "Containers:  main, image=alpine:latest" in inspected.stdout
  assert "main:8080/tcp <- http://localhost:41000" in inspected.stdout
  assert "harbor logs -f ports-demo" in inspected.stdout
  assert "Last action:" in inspected.stdout
  assert "subdomain: ports" in inspected.stdout
  assert "Note:" not in inspected.stdout


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


def test_inspect_shows_config_status(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0

  before = harbor_env.run("inspect", BASIC)
  assert before.returncode == 0, before.stderr
  assert "admin_user: (required)" in before.stdout
  assert "admin_pass: (secret)" in before.stdout

  assert harbor_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0
  after = harbor_env.run("inspect", BASIC)
  assert after.returncode == 0, after.stderr
  assert "admin_user: alice" in after.stdout
  assert "admin_pass: (secret)" in after.stdout


def test_inspect_notes_when_the_source_manifest_has_drifted(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  source = harbor_env.root / "apps" / f"{BASIC}.happ" / "manifest.toml"
  source.write_text(source.read_text() + "\n# edited after staging\n")

  inspected = harbor_env.run("inspect", BASIC)
  assert inspected.returncode == 0, inspected.stderr
  assert "Note:" in inspected.stdout
  assert (
    f"manifest has changed, `harbor stage {BASIC}` may be required to reflect "
    f"changes" in inspected.stdout
  )


def test_inspect_by_path_shows_declared_config_without_installing(harbor_env):
  happ = harbor_env.root / "apps" / f"{BASIC}.happ"
  inspected = harbor_env.run("inspect", str(happ))
  assert inspected.returncode == 0, inspected.stderr
  assert "admin_user: (required)" in inspected.stdout
  assert "admin_pass: (secret)" in inspected.stdout
  assert not harbor_env.app_logtab(BASIC).exists()
  assert "Note:" not in inspected.stdout
  assert "Last action:" not in inspected.stdout
  assert "State:" not in inspected.stdout


def test_logs_accepts_native_flags_before_app(harbor_env):
  assert harbor_env.run("start", "ports-demo").returncode == 0
  # Fake docker ignores unknown compose args; success means argparse accepted order.
  result = harbor_env.run("logs", "-f", "--tail", "10", "ports-demo")
  assert result.returncode == 0, result.stderr
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "logs", "--follow", "--tail", "10"] in calls


# --- commands --------------------------------------------------------------


def test_cmd_lists_and_runs_manifest_commands(harbor_env):
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
desc = "Print pong"

[commands.argv]
cmd = ["echo", "hello"]
desc = "List-form command"
"""
  )

  assert harbor_env.run("start", "cmd-demo").returncode == 0

  listed = harbor_env.run("cmd", "cmd-demo")
  assert listed.returncode == 0, listed.stderr
  assert listed.stdout.splitlines()[0].split() == [
    "COMMAND",
    "DESCRIPTION",
    "RUN_UNIT",
  ]
  assert "ping" in listed.stdout
  assert "Print pong" in listed.stdout
  assert "argv" in listed.stdout

  ran = harbor_env.run("cmd", "cmd-demo", "ping", "extra")
  assert ran.returncode == 0, ran.stderr
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
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

  list_form = harbor_env.run("cmd", "cmd-demo", "argv", "world")
  assert list_form.returncode == 0, list_form.stderr
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert ["compose", "exec", "main", "echo", "hello", "world"] in calls


def test_cmd_uses_run_when_container_is_stopped(harbor_env):
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

  not_staged = harbor_env.run("cmd", "cmd-demo")
  assert not_staged.returncode == 1
  assert "not staged" in not_staged.stderr

  assert harbor_env.run("stage", "cmd-demo").returncode == 0
  one_off = harbor_env.run("cmd", "cmd-demo", "ping", "extra")
  assert one_off.returncode == 0, one_off.stderr
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
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

  assert harbor_env.run("start", "cmd-demo").returncode == 0
  missing = harbor_env.run("cmd", "cmd-demo", "nope")
  assert missing.returncode == 1
  assert "Unknown command 'nope'" in missing.stderr
  assert "harbor cmd cmd-demo" in missing.stderr


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


def test_config_before_staging_reads_the_bundle(harbor_env):
  """Values can be set before the first stage; the source is the only manifest
  there is, and `stage` keeps whatever is already on file."""
  listed = harbor_env.run("config", BASIC)
  assert listed.returncode == 0, listed.stderr
  assert "admin_user" in listed.stdout

  early = harbor_env.run("config", BASIC, "--set", "admin_user=alice")
  assert early.returncode == 0, early.stderr

  assert harbor_env.run("stage", BASIC).returncode == 0
  kept = harbor_env.run("config", BASIC, "--get", "admin_user")
  assert kept.stdout.strip() == "alice"


def test_binding_before_staging_applies_at_the_first_start(harbor_env):
  """The bind is recorded against the source's manifest, so the very first
  stage already has it -- no start-then-bind-then-restage round trip."""
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()

  bound = harbor_env.run("config", app_id, "--bind", "hostvol1=media")
  assert bound.returncode == 0, bound.stderr

  assert harbor_env.run("start", app_id).returncode == 0
  link = harbor_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.resolve() == host_path


def test_config_of_a_staged_app_reads_the_run_copy(harbor_env):
  """Not the source, which may have moved on since: the run copy is what the
  app will actually start with."""
  assert harbor_env.run("stage", BASIC).returncode == 0

  manifest = harbor_env.root / "apps" / f"{BASIC}.happ" / "manifest.toml"
  manifest.write_text(
    manifest.read_text().replace("[volumes]", "since_staging = {}\n\n[volumes]")
  )
  # The source now declares it; the installed app does not.
  assert "since_staging" in harbor_env.run("inspect", str(manifest.parent)).stdout
  assert "since_staging" not in harbor_env.run("config", BASIC).stdout

  refused = harbor_env.run("config", BASIC, "--set", "since_staging=x")
  assert refused.returncode == 1
  assert "No config since_staging" in refused.stderr


def test_config_refuses_an_app_it_has_no_manifest_for(harbor_env):
  gone = harbor_env.run("config", "no-such-app")
  assert gone.returncode == 1
  assert "No app found" in gone.stderr


def test_assigning_a_route_still_needs_staging(harbor_env):
  """A provider needs the allocated host port, which only staging hands out."""
  too_early = harbor_env.run("config", "routes-demo", "--route", "main=web")
  assert too_early.returncode == 1
  assert "harbor stage routes-demo" in too_early.stderr


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


def test_config_set_subdomain_overrides_the_manifest(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.read_db()["routes"][app_id]["web"]["subdomain"] == "web-ports"

  running = harbor_env.run("config", app_id, "--set", "subdomain=lab")
  assert running.returncode == 1
  assert f"harbor stop {app_id}" in running.stderr
  assert harbor_env.read_db()["routes"][app_id]["web"]["subdomain"] == "web-ports"

  assert harbor_env.run("stop", app_id).returncode == 0
  set_result = harbor_env.run("config", app_id, "--set", "subdomain=lab")
  assert set_result.returncode == 0, set_result.stderr

  got = harbor_env.run("config", app_id, "--get", "subdomain")
  assert got.stdout.strip() == "lab"
  assert harbor_env.read_db()["routes"][app_id]["web"]["subdomain"] == "web-lab"
  assert harbor_env.read_db()["routes"][app_id]["admin"]["subdomain"] == "admin-lab"

  listed = harbor_env.run("config", app_id)
  assert listed.returncode == 0, listed.stderr
  assert "subdomain" in listed.stdout
  assert "lab" in listed.stdout


def test_config_set_subdomain_rejects_a_dotted_name(harbor_env):
  assert harbor_env.run("stage", "ports-demo").returncode == 0
  result = harbor_env.run("config", "ports-demo", "--set", "subdomain=foo.bar")
  assert result.returncode == 1
  assert "foo.bar" in result.stderr
  assert "identifier" in result.stderr


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


def test_decrypt_round_trips_a_stored_secret(harbor_env):
  secret = "correct horse battery staple"
  key = "backup.api_key"
  assert (
    harbor_env.run("config-sys", "--stdin", key, input=f"{secret}\n").returncode == 0
  )

  blob = harbor_env.read_db()["system"]["secrets"][key]
  assert secret not in blob

  result = harbor_env.run("decrypt", input=f"{blob}\n")
  assert result.returncode == 0, result.stderr
  assert result.stdout.strip() == secret


def test_decrypt_tolerates_surrounding_whitespace(harbor_env):
  # A blob pasted out of the logtab or a shell pipeline arrives padded.
  harbor_env.run("config-sys", "--set", "k=hunter2")
  blob = harbor_env.read_db()["system"]["secrets"]["k"]

  result = harbor_env.run("decrypt", input=f"   {blob}  \n")
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
def test_decrypt_refuses_what_it_cannot_authenticate(harbor_env, blob):
  result = harbor_env.run("decrypt", input=f"{blob}\n")

  assert result.returncode == 1
  assert "Could not decrypt" in result.stderr
  # Nothing plausible-looking leaks onto stdout on failure.
  assert result.stdout.strip() == ""


def test_decrypt_refuses_empty_stdin(harbor_env):
  result = harbor_env.run("decrypt", input="\n")
  assert result.returncode == 1
  assert "Nothing on stdin" in result.stderr


def test_decrypt_refuses_when_there_is_no_master_key(harbor_env):
  # Without a key the noop engine would echo the input back and call it success.
  (harbor_env.root / "master.key").write_text("")
  blob = FernetCryptoEngine("0" * 64).encrypt("hunter2")

  result = harbor_env.run("decrypt", input=f"{blob}\n")
  assert result.returncode == 1
  assert "No master key" in result.stderr
  assert "hunter2" not in result.stdout


def test_host_bind_one_shot_via_start(harbor_env):
  app_id = "host-volumes"
  blocked = harbor_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "Bind with `harbor config host-volumes --bind hostvol1=" in blocked.stderr

  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  started = harbor_env.run("start", app_id, "--bind", "hostvol1=media")
  assert started.returncode == 0, started.stderr
  link = harbor_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.is_symlink()
  assert link.resolve() == host_path


def test_host_links_belong_to_the_run_not_the_stage(harbor_env):
  """`bind` records; `start` links; `stop` unlinks.

  A bind that only ever reached the config store was the original bug: compose
  mounts `volumes/host/<name>` regardless, so docker created it as an empty
  directory and the app came up against that instead of the host path.
  """
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  host_root = harbor_env.run_root / app_id / "volumes" / "host"
  link = host_root / "hostvol1"

  assert harbor_env.run("stage", app_id).returncode == 0
  assert not host_root.exists(), "staging has no business linking somebody's data"

  bound = harbor_env.run("config", app_id, "--bind", "hostvol1=media")
  assert bound.returncode == 0, bound.stderr
  assert not host_root.exists()

  started = harbor_env.run("start", app_id)
  assert started.returncode == 0, started.stderr
  assert link.is_symlink()
  assert link.resolve() == host_path

  assert harbor_env.run("stop", app_id).returncode == 0
  assert not host_root.exists()
  assert host_path.is_dir(), "unlinking must not touch what was linked to"


def test_restaging_leaves_the_binds_alone(harbor_env):
  """Staging rebuilds the run dir, but a bind survives it and still applies."""
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  assert harbor_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  assert harbor_env.run("stage", app_id).returncode == 0
  assert harbor_env.run("start", app_id).returncode == 0
  link = harbor_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.resolve() == host_path


def test_rebinding_takes_effect_at_the_next_start(harbor_env):
  """A second bind moves the link; the old target is left alone."""
  app_id = "host-volumes"
  first = harbor_env.root / "external-data"
  second = harbor_env.root / "other-data"
  first.mkdir()
  second.mkdir()
  assert harbor_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0

  link = harbor_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  rebound = harbor_env.run("config", app_id, "--bind", "hostvol1=other")
  assert rebound.returncode == 0, rebound.stderr
  assert "is running" in rebound.stderr
  assert link.resolve() == first, "a running app's links must not move"

  assert harbor_env.run("stop", app_id).returncode == 0
  assert harbor_env.run("start", app_id).returncode == 0
  assert link.resolve() == second
  assert first.is_dir()


def test_start_replaces_whatever_is_in_the_host_directory(harbor_env):
  """`host/` is rebuilt from the binds, so nothing stale can survive a start.

  Both cases at once: the empty directory docker leaves when it mounts a bind
  source that was not linked, and a link pointing somewhere the bind no longer
  says.
  """
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  assert harbor_env.run("stage", app_id).returncode == 0
  assert harbor_env.run("config", app_id, "--bind", "hostvol1=media").returncode == 0

  link = harbor_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  link.mkdir(parents=True)  # what docker left behind

  assert harbor_env.run("start", app_id).returncode == 0
  assert link.is_symlink()
  assert link.resolve() == host_path

  assert harbor_env.run("stop", app_id).returncode == 0
  link.parent.mkdir(parents=True, exist_ok=True)
  link.symlink_to(harbor_env.root)  # a link from some earlier bind

  assert harbor_env.run("start", app_id).returncode == 0
  assert link.resolve() == host_path


def test_start_refuses_when_a_bound_path_has_gone(harbor_env):
  """A bind whose host path is no longer there must not start the app.

  `bind` checks the path at bind time, so the way to reach this is a share
  that stopped being mounted -- exactly when starting anyway is worst, since
  docker would recreate the path as an empty directory and the app would come
  up against an empty volume instead of failing.
  """
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  assert harbor_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_path)

  blocked = harbor_env.run("start", app_id)
  assert blocked.returncode == 1
  assert "path does not exist" in blocked.stderr
  assert str(host_path) in blocked.stderr
  assert not host_path.exists(), "a failed start must not recreate the host path"

  # The bind is still on file, so putting the path back is the whole fix.
  host_path.mkdir()
  assert harbor_env.run("start", app_id).returncode == 0


def test_missing_host_volume_path_blocks_stage(harbor_env):
  """A bound host volume whose path is gone must refuse restaging."""
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  assert harbor_env.run("start", app_id, "--bind", "hostvol1=media").returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_path)
  blocked = harbor_env.run("stage", app_id)
  assert blocked.returncode == 1
  assert "path does not exist" in blocked.stderr
  assert str(host_path) in blocked.stderr


def test_host_volume_may_be_a_file(harbor_env):
  """A host volume can point at a single file, not only a directory."""
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.write_text("just a file")

  started = harbor_env.run("start", app_id, "--bind", "hostvol1=media")
  assert started.returncode == 0, started.stderr
  link = harbor_env.run_root / app_id / "volumes" / "host" / "hostvol1"
  assert link.is_symlink()
  assert link.resolve() == host_path
  assert link.resolve().is_file()


def test_require_mount_refuses_unmounted_path(harbor_env):
  """require_mount catches an empty mount-point directory."""
  with open(harbor_env.config, "a") as f:
    f.write('\n[host_volume.nfs]\npath = "external-data"\nrequire_mount = true\n')
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()

  assert harbor_env.run("stage", "host-volumes").returncode == 0
  refused = harbor_env.run("config", "host-volumes", "--bind", "hostvol1=nfs")
  assert refused.returncode == 1
  assert "not mounted" in refused.stderr


def test_require_mount_blocks_stage_when_unmounted(harbor_env):
  """A previously bound require_mount volume blocks restaging if unmounted."""
  # Bind against a real mount first (/), then retarget the tag at an ordinary
  # directory so restaging sees require_mount fail through ConfigIssue.
  with open(harbor_env.config, "a") as f:
    f.write('\n[host_volume.rootfs]\npath = "/"\nrequire_mount = true\n')

  assert harbor_env.run("stage", "host-volumes").returncode == 0
  assert (
    harbor_env.run("config", "host-volumes", "--bind", "hostvol1=rootfs").returncode
    == 0
  )

  # Rewrite the same tag to a non-mount path without clearing the bind.
  config_text = harbor_env.config.read_text()
  harbor_env.config.write_text(
    config_text.replace('path = "/"', 'path = "external-data"')
  )
  (harbor_env.root / "external-data").mkdir(exist_ok=True)

  blocked = harbor_env.run("stage", "host-volumes")
  assert blocked.returncode == 1
  assert "not mounted" in blocked.stderr


def test_readonly_host_volume_refuses_writable_app_volume(harbor_env):
  """A readonly host volume may only be bound by a readonly app volume."""
  with open(harbor_env.config, "a") as f:
    f.write('\n[host_volume.ro_media]\npath = "ro-data"\nreadonly = true\n')
  (harbor_env.root / "ro-data").mkdir()

  assert harbor_env.run("stage", "host-volumes").returncode == 0
  refused = harbor_env.run("config", "host-volumes", "--bind", "hostvol1=ro_media")
  assert refused.returncode == 1
  assert "readonly" in refused.stderr


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

  needs_config_row = _ps_row(harbor_env.run("ps").stdout, app_id)
  assert needs_config_row[:4] == [app_id, "-", "missing", "0"]

  configured = harbor_env.run("config", app_id, "--set", "api_key=sekrit")
  assert configured.returncode == 0, configured.stderr
  started = harbor_env.run("start", app_id)
  assert started.returncode == 0, started.stderr


def test_a_required_non_secret_value_blocks_start_and_shows_as_missing_config(
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

  row = _ps_row(harbor_env.run("ps").stdout, app_id)
  assert row[:4] == [app_id, "-", "missing", "0"]

  assert harbor_env.run("config", app_id, "--set", "hostname=box").returncode == 0
  assert harbor_env.run("start", app_id).returncode == 0


# --- ps status accuracy ----------------------------------------------------
#
# `start` establishes preconditions before it judges readiness: stage() generates
# config defaults and reallocates every route, *then* evaluates blockers. `ps`
# calls load_run_data() with neither having run, so anything start repairs itself
# must not be reported as something the operator has to fix.


def test_ps_reports_config_readiness_and_volume_count(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  row = _ps_row(harbor_env.run("ps").stdout, BASIC)
  assert row[1:4] == ["-", "missing", "3"]

  assert harbor_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0
  row = _ps_row(harbor_env.run("ps").stdout, BASIC)
  assert row[1:4] == ["-", "ready", "3"]


def test_unallocated_routes_are_not_reported_as_missing_config(harbor_env):
  """Unallocated routes are self-healing; CONFIG must stay ready."""
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  # Route rows gone while the run dir survives -- the pre-start allocation state.
  HarborCtx(load_config_file(harbor_env.config)).harbor_db.clear_routes(app_id)
  assert (harbor_env.run_root / app_id / "compose.yml").is_file()

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert _ps_row(listed.stdout, app_id)[2] == "ready"

  # ...and it is genuinely startable, configuring nothing.
  assert harbor_env.run("start", app_id).returncode == 0


def test_an_unmet_bind_is_reported_as_missing_config(harbor_env):
  """A host volume whose path has gone is the operator's to fix."""
  app_id = "host-volumes"
  host_path = harbor_env.root / "external-data"
  host_path.mkdir()
  assert harbor_env.run("stage", app_id).returncode == 0
  assert harbor_env.run("config", app_id, "--bind", "hostvol1=media").returncode == 0
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  shutil.rmtree(host_path)

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  assert _ps_row(listed.stdout, app_id)[2] == "missing"


def test_an_unloadable_app_is_not_reported_as_missing_config(harbor_env):
  """A staged happ that will not parse is unknown, not unconfigured."""
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.run("stop", app_id).returncode == 0

  (harbor_env.run_root / app_id / "happ" / "manifest.toml").write_text("not toml {[")

  listed = harbor_env.run("ps")
  assert listed.returncode == 0, listed.stderr
  row = _ps_row(listed.stdout, app_id)
  assert row[1:4] == ["-", "-", "-"]


# --- doctor ----------------------------------------------------------------


def test_doctor_reports_orphaned_routes(harbor_env):
  """Routes are the only per-app state harbordb still holds.

  Config and binds live in config/<app_id>.logtab, so a harbordb entry with no
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
        "scheme": "http",
      }
    }
  )

  ps = harbor_env.run("ps")
  assert _ps_row(ps.stdout, "io.example.abandoned")[1:4] == ["-", "-", "-"]

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
      lambda ctx, tag: provider,
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
  assert (root / "config").is_dir()
  for kind in VOLUME_KINDS:
    assert (root / "volumes" / kind).is_dir(), kind
  for name in VAR_DIRS:
    assert (root / "var" / name).is_dir(), name

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
  assert "Routes:" in started.stdout

  ps = harbor_env.run("ps")
  assert ps.returncode == 0, ps.stderr
  assert "demo-routes" in ps.stdout


# --- activity log ----------------------------------------------------------


def test_last_action_is_read_in_one_pass(harbor_env):
  """`ps` reports every app's action from a single read of the shared log."""
  assert harbor_env.run("start", "ports-demo").returncode == 0
  assert harbor_env.run("start", "routes-demo").returncode == 0

  ctx = HarborCtx(load_config_file(harbor_env.config))
  assert {k: v[1] for k, v in read_app_actions(ctx).items()} == {
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

  ctx = HarborCtx(load_config_file(harbor_env.config))
  assert read_last_app_action(app_id, ctx) == "started"

  assert harbor_env.run("rm", app_id, "-y").returncode == 0

  assert not (harbor_env.run_root / app_id).exists()
  assert read_last_app_action(app_id, ctx) == "removed"
  assert f"apps/{app_id}/status" in ctx.activity_log.load()


# --- running as root -------------------------------------------------------


def test_refuse_root_is_quiet_for_an_ordinary_user(monkeypatch):
  monkeypatch.setattr(os, "geteuid", lambda: 1000)
  assert refuse_root("harbor") is None


def test_refuse_root_names_who_and_what_to_do(monkeypatch):
  monkeypatch.setattr(os, "geteuid", lambda: 0)
  with pytest.raises(RuntimeError) as raised:
    refuse_root("the harbor daemon")
  assert "the harbor daemon" in str(raised.value)
  assert "sudo" in str(raised.value)


def test_every_command_refuses_to_run_as_root(harbor_env, monkeypatch):
  """`init` included: it is the command that would create the root-owned tree."""
  monkeypatch.setattr(os, "geteuid", lambda: 0)
  for argv in (["init"], ["ps"], ["snapshot", BASIC]):
    refused = harbor_env.run(*argv)
    assert refused.returncode == 1, argv
    assert "refuses to run as root" in refused.stderr, argv


# --- activity --------------------------------------------------------------


def _seed_activity(harbor_env, **kwargs):
  """File one activity run straight through the lib, as harbord's job runner
  would. CLI flows do not record activity themselves -- only daemon jobs do."""
  from datetime import UTC, datetime, timedelta

  from harbor.lib import activity
  from harbor.lib.apps import AppID

  ctx = HarborCtx(load_config_file(harbor_env.config))
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


def test_activity_reports_nothing_when_empty(harbor_env):
  result = harbor_env.run("activity")
  assert result.returncode == 0
  assert "No recorded activity" in result.stdout


def test_activity_lists_recorded_runs(harbor_env):
  _seed_activity(harbor_env, verb="start", app="ports-demo")
  _seed_activity(harbor_env, verb="stop", app="ports-demo", status="error")

  result = harbor_env.run("activity")
  assert result.returncode == 0
  assert "start ports-demo" in result.stdout
  assert "stop ports-demo" in result.stdout
  # Newest first.
  assert result.stdout.index("stop") < result.stdout.index("start")


def test_activity_show_prints_a_run_file(harbor_env):
  _seed_activity(harbor_env, output="the captured output")
  result = harbor_env.run("activity", "--show")
  assert result.returncode == 0
  assert "the captured output" in result.stdout


def test_activity_filters_by_app_stem(harbor_env):
  _seed_activity(harbor_env, app="ports-demo")
  _seed_activity(harbor_env, app="routes-demo")
  result = harbor_env.run("activity", "ports-demo")
  assert "ports-demo" in result.stdout
  assert "routes-demo" not in result.stdout
