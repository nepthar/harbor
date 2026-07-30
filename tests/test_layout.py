"""The `run/<app_id>/` layout and the stage / start / stop / rm lifecycle.

Everything here goes through the CLI against the `harbor_env` fixture, whose
fake docker is the only docker these tests are allowed to see.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

BASIC = "io.p2net.basic-features"


def _write_happ(harbor_env, app_id: str, manifest: str) -> Path:
  """Create (or overwrite) a catalog entry with the given manifest."""
  happ = harbor_env.root / "apps" / f"{app_id}.happ"
  happ.mkdir(parents=True, exist_ok=True)
  (happ / "manifest.toml").write_text(manifest)
  return happ


def _compose(harbor_env, app_id: str) -> dict:
  return yaml.safe_load((harbor_env.run_root / app_id / "compose.yml").read_text())


def _volumes_manifest(volumes: str) -> str:
  return f"""\
[app]
version = "1"

[volumes]
{volumes}

[run.main]
image   = "alpine:latest"
cmd     = ["true"]
volumes = {{ }}
"""


# --- stage ------------------------------------------------------------------


def test_stage_copies_the_happ_into_the_run_dir(harbor_env):
  """What is installed is a fact on disk, not a pointer to something else."""
  app_id = "ports-demo"
  staged = harbor_env.run("stage", app_id)
  assert staged.returncode == 0, staged.stderr

  run_dir = harbor_env.run_root / app_id
  catalog = harbor_env.root / "apps" / f"{app_id}.happ"
  copied = run_dir / "happ" / "manifest.toml"
  assert copied.is_file()
  assert not copied.is_symlink()
  assert copied.read_text() == (catalog / "manifest.toml").read_text()
  assert not (run_dir / "source").exists()
  assert (run_dir / "config.logtab").is_file()
  assert (run_dir / "compose.yml").is_file()


def test_editing_the_catalog_then_restaging_recopies(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("stage", app_id).returncode == 0

  catalog = harbor_env.root / "apps" / f"{app_id}.happ" / "manifest.toml"
  catalog.write_text(
    catalog.read_text().replace('version      = "0.1.0"', 'version = "9"')
  )

  assert harbor_env.run("stage", app_id).returncode == 0
  assert (
    'version = "9"'
    in (harbor_env.run_root / app_id / "happ" / "manifest.toml").read_text()
  )


def test_editing_the_run_copy_is_lost_on_the_next_stage(harbor_env):
  """The run copy is harbor's output, never its input: apps/ is the source."""
  app_id = "ports-demo"
  assert harbor_env.run("stage", app_id).returncode == 0

  copied = harbor_env.run_root / app_id / "happ" / "manifest.toml"
  copied.write_text(copied.read_text() + "\n# hand edit\n")
  (harbor_env.run_root / app_id / "happ" / "stowaway.txt").write_text("hi")

  assert harbor_env.run("stage", app_id).returncode == 0
  assert "# hand edit" not in copied.read_text()
  assert not (harbor_env.run_root / app_id / "happ" / "stowaway.txt").exists()


def test_stage_preserves_config_and_volume_contents(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  assert harbor_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0

  payload = harbor_env.volumes_root / "data" / BASIC / "config" / "state.txt"
  payload.write_text("precious")

  assert harbor_env.run("stage", BASIC).returncode == 0

  got = harbor_env.run("config", BASIC, "--get", "admin_user")
  assert got.stdout.strip() == "alice"
  assert payload.read_text() == "precious"


def test_stage_by_path_links_it_into_the_catalog(harbor_env):
  app_id = "ports-demo"
  elsewhere = harbor_env.root / "checkout" / f"{app_id}.happ"
  elsewhere.parent.mkdir()
  (harbor_env.root / "apps" / f"{app_id}.happ").rename(elsewhere)

  staged = harbor_env.run("stage", str(elsewhere))
  assert staged.returncode == 0, staged.stderr

  entry = harbor_env.root / "apps" / f"{app_id}.happ"
  assert entry.is_symlink()
  assert entry.readlink() == elsewhere.resolve()
  assert (harbor_env.run_root / app_id / "happ" / "manifest.toml").is_file()


def test_stage_by_path_refuses_a_conflicting_catalog_entry(harbor_env):
  """The catalog entry is the source of truth; a second source is refused."""
  app_id = "ports-demo"
  other = harbor_env.root / "checkout" / f"{app_id}.happ"
  other.parent.mkdir()
  _write_happ(
    harbor_env, "scratch", '[app]\nversion = "1"\n\n[run.main]\nimage = "alpine"\n'
  )
  (harbor_env.root / "apps" / "scratch.happ").rename(other)

  refused = harbor_env.run("stage", str(other))
  assert refused.returncode == 1
  assert "already in the catalog" in refused.stderr


def test_stage_refuses_while_containers_are_running(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0

  refused = harbor_env.run("stage", app_id)
  assert refused.returncode == 1
  assert f"harbor stop {app_id}" in refused.stderr


def test_stage_refuses_when_config_is_gone_but_data_remains(harbor_env):
  """Deleting the run dir by hand must not silently regenerate secrets.

  Fresh `auto` secrets against data that expects the old ones produce an app
  that fails to authenticate for reasons nothing in the error explains.
  """
  assert harbor_env.run("stage", BASIC).returncode == 0
  (harbor_env.volumes_root / "data" / BASIC / "config" / "db").write_text("rows")
  shutil.rmtree(harbor_env.run_root / BASIC)

  refused = harbor_env.run("stage", BASIC)
  assert refused.returncode == 1
  assert "volume data but no config" in refused.stderr
  assert f"harbor rm {BASIC}" in refused.stderr


# --- volume links -----------------------------------------------------------


def test_app_links_are_relative_and_managed_links_are_absolute(harbor_env):
  """Relative app links survive a moved run dir; managed roots may be elsewhere."""
  assert harbor_env.run("stage", BASIC).returncode == 0
  volumes = harbor_env.run_root / BASIC / "volumes"

  app_link = volumes / "app" / "bin"
  assert app_link.is_symlink()
  assert app_link.readlink() == Path("../../happ/bin")
  assert app_link.resolve() == (harbor_env.run_root / BASIC / "happ" / "bin").resolve()

  data_link = volumes / "data" / "config"
  assert data_link.is_symlink()
  assert data_link.readlink().is_absolute()
  assert data_link.readlink() == harbor_env.volumes_root / "data" / BASIC / "config"

  temp_link = volumes / "temp" / "cache"
  assert temp_link.readlink() == harbor_env.volumes_root / "temp" / BASIC / "cache"


def test_app_volumes_are_mounted_read_only(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  mounts = _compose(harbor_env, BASIC)["services"]["main"]["volumes"]

  assert "./volumes/app/bin:/myapp/bin:ro" in mounts
  assert "./volumes/data/config:/myapp/config" in mounts
  assert "./volumes/temp/cache:/myapp/cache" in mounts


def test_readonly_false_on_an_app_volume_is_a_manifest_error(harbor_env):
  """An author who wrote it meant something, so say it is impossible."""
  _write_happ(
    harbor_env,
    "bad-app-volume",
    _volumes_manifest('assets = { kind = "app", readonly = false }'),
  )

  refused = harbor_env.run("stage", "bad-app-volume")
  assert refused.returncode == 1
  assert "app volumes are always mounted read-only" in refused.stderr
  assert "assets" in refused.stderr
  assert not (harbor_env.run_root / "bad-app-volume").exists()


def test_a_dropped_volume_keeps_its_data_and_is_reported(harbor_env):
  app_id = "vol-churn"
  _write_happ(
    harbor_env,
    app_id,
    _volumes_manifest('one = { kind = "data" }\ntwo = { kind = "data" }'),
  )
  assert harbor_env.run("stage", app_id).returncode == 0
  payload = harbor_env.volumes_root / "data" / app_id / "two" / "keep.txt"
  payload.write_text("still here")

  _write_happ(harbor_env, app_id, _volumes_manifest('one = { kind = "data" }'))
  restaged = harbor_env.run("stage", app_id)
  assert restaged.returncode == 0, restaged.stderr

  assert not (harbor_env.run_root / app_id / "volumes" / "data" / "two").exists()
  assert payload.read_text() == "still here"
  assert "two" in restaged.stderr


def test_an_added_volume_is_created_empty(harbor_env):
  app_id = "vol-churn"
  _write_happ(harbor_env, app_id, _volumes_manifest('one = { kind = "data" }'))
  assert harbor_env.run("stage", app_id).returncode == 0

  _write_happ(
    harbor_env,
    app_id,
    _volumes_manifest('one = { kind = "data" }\ntwo = { kind = "data" }'),
  )
  assert harbor_env.run("stage", app_id).returncode == 0

  added = harbor_env.volumes_root / "data" / app_id / "two"
  assert added.is_dir()
  assert list(added.iterdir()) == []


def test_changing_a_volumes_kind_is_refused(harbor_env):
  """The bytes live under the old kind's root; moving them silently is worse."""
  app_id = "vol-churn"
  _write_happ(harbor_env, app_id, _volumes_manifest('one = { kind = "data" }'))
  assert harbor_env.run("stage", app_id).returncode == 0

  _write_happ(harbor_env, app_id, _volumes_manifest('one = { kind = "bulk" }'))
  refused = harbor_env.run("stage", app_id)
  assert refused.returncode == 1
  assert "changed kind from data to bulk" in refused.stderr
  assert (harbor_env.run_root / app_id / "volumes" / "data" / "one").is_symlink()


# --- config -----------------------------------------------------------------


def test_config_round_trips_through_the_run_dir(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  assert harbor_env.run("config", BASIC, "--set", "admin_user=alice").returncode == 0

  logtab = (harbor_env.run_root / BASIC / "config.logtab").read_text()
  assert "config/admin_user" in logtab
  assert "meta/origin" in logtab
  assert "meta/staged_at" in logtab

  # ...and nothing about this app is left in the central db.
  assert BASIC not in harbor_env.read_db().get("apps", {})


def test_start_set_is_the_one_shot_for_an_unstaged_app(harbor_env):
  started = harbor_env.run("start", BASIC, "--set", "admin_user=alice")
  assert started.returncode == 0, started.stderr

  got = harbor_env.run("config", BASIC, "--get", "admin_user")
  assert got.stdout.strip() == "alice"


def test_restaging_does_not_regenerate_an_existing_auto_secret(harbor_env):
  """The worst possible bug here: a new secret against data expecting the old.

  `admin_pass` is `{ secret = true, default = "auto" }`, so it is minted on the
  first stage and must survive every one after it.
  """
  assert harbor_env.run("stage", BASIC).returncode == 0
  first = harbor_env.run(
    "config", BASIC, "--get", "admin_pass", "--show-secret"
  ).stdout.strip()
  assert first

  assert harbor_env.run("stage", BASIC).returncode == 0
  assert harbor_env.run("stage", BASIC).returncode == 0

  again = harbor_env.run(
    "config", BASIC, "--get", "admin_pass", "--show-secret"
  ).stdout.strip()
  assert again == first


# --- rm ---------------------------------------------------------------------


def test_rm_removes_the_run_dir_volumes_and_routes(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  assert harbor_env.read_db()["routes"][app_id]

  removed = harbor_env.run("rm", app_id, "-y")
  assert removed.returncode == 0, removed.stderr

  assert not (harbor_env.run_root / app_id).exists()
  for kind in ("data", "temp", "bulk", "logs"):
    assert not (harbor_env.volumes_root / kind / app_id).exists()
  assert app_id not in harbor_env.read_db().get("routes", {})
  # The catalog entry is the reinstall path, so it survives on purpose.
  assert (harbor_env.root / "apps" / f"{app_id}.happ").is_dir()


def test_rm_needs_confirmation_and_says_it_cannot_be_undone(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0

  declined = harbor_env.run("rm", app_id, input="n\n")
  assert declined.returncode == 0, declined.stderr
  assert "cannot be undone" in declined.stdout
  assert "Nothing removed" in declined.stdout
  assert (harbor_env.run_root / app_id).is_dir()

  confirmed = harbor_env.run("rm", app_id, input="y\n")
  assert confirmed.returncode == 0, confirmed.stderr
  assert not (harbor_env.run_root / app_id).exists()


def test_rm_reports_external_binds_it_leaves_alone(harbor_env):
  app_id = "ext-volumes"
  host_volume = harbor_env.root / "external-data"
  host_volume.mkdir()
  assert (
    harbor_env.run("start", app_id, "--bind", f"extvol1={host_volume}").returncode == 0
  )

  removed = harbor_env.run("rm", app_id, input="y\n")
  assert removed.returncode == 0, removed.stderr
  assert str(host_volume) in removed.stdout
  assert host_volume.is_dir()


def test_rm_then_start_is_a_clean_reinstall(harbor_env):
  assert harbor_env.run("start", BASIC, "--set", "admin_user=alice").returncode == 0
  minted = harbor_env.run(
    "config", BASIC, "--get", "admin_pass", "--show-secret"
  ).stdout.strip()

  assert harbor_env.run("rm", BASIC, "-y").returncode == 0

  restarted = harbor_env.run("start", BASIC, "--set", "admin_user=bob")
  assert restarted.returncode == 0, restarted.stderr
  assert (harbor_env.run_root / BASIC / "happ" / "manifest.toml").is_file()

  # Config and data went together, so a fresh secret is correct here.
  fresh = harbor_env.run(
    "config", BASIC, "--get", "admin_pass", "--show-secret"
  ).stdout.strip()
  assert fresh and fresh != minted
