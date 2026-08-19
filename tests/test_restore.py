"""Snapshot and restore: refusals, the config round trip, and data volumes.

Copying data volumes is done by a throwaway container rather than by this
process, because containers write their files as root. The fake docker runs the
container's script on the host, so what these tests pin down is the command
harbor builds and the data that survives the round trip; whether the *real*
container preserves root ownership is a live test in docs/testing.md.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
import yaml

from harbor.lib.apps import read_last_app_action
from harbor.lib.config import load_config_file
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle.rootfs import ROOTFS_IMAGE, run_as_root

BASIC = "io.p2net.basic-features"


def _snapshot(harbor_env, app_id: str, label: str) -> str:
  """Stop `app_id`, snapshot it, and return the snapshot name."""
  assert harbor_env.run("stop", app_id).returncode == 0
  taken = harbor_env.run("snapshot", app_id, "--label", label)
  assert taken.returncode == 0, taken.stderr
  written = Path(taken.stdout.split("written to ")[1].strip())
  name = written.name.removesuffix(".tar.gz")
  assert written.is_file(), taken.stdout
  assert not (written.parent / name).exists()
  return name


def _root_runs(harbor_env) -> list[list[str]]:
  """Every `docker run` the fake recorded, oldest first."""
  return [
    entry["args"]
    for entry in map(json.loads, harbor_env.docker_log.read_text().splitlines())
    if entry["args"][0] == "run"
  ]


def _mounts(args: list[str]) -> list[str]:
  return [args[i + 1] for i, arg in enumerate(args) if arg == "-v"]


def test_snapshot_copies_data_volumes_in_a_container(harbor_env):
  """The copy harbor cannot do itself: `cp -a` over root-owned volume files."""
  assert harbor_env.run("stage", BASIC).returncode == 0
  volume = harbor_env.volumes_root / "data" / BASIC / "config"
  (volume / "db.txt").write_text("v1")
  # cp -a must not follow this: dereferencing a symlink inside a volume is how
  # a snapshot silently becomes a different thing than the volume was.
  (volume / "current").symlink_to("db.txt")

  taken = harbor_env.run("snapshot", BASIC, "--label", "one")
  assert taken.returncode == 0, taken.stderr
  archive = Path(taken.stdout.split("written to ")[1].strip())

  copy = next(args for args in _root_runs(harbor_env) if "cp -a -- " in args[-1])
  staged = harbor_env.root / "temp" / "current_snapshot" / "volumes" / "data"
  assert copy[:2] == ["run", "--rm"]
  assert copy[-3:-1] == ["sh", "-c"]
  assert copy[copy.index("sh") - 1] == ROOTFS_IMAGE
  # Bound at their own absolute host paths, which is what lets the script name
  # host paths verbatim.
  assert _mounts(copy) == [f"{volume}:{volume}", f"{staged}:{staged}"]
  assert copy[-1] == f"cp -a -- {volume} {staged}"

  # The archive is written by this process, not the container, so it belongs to
  # whoever ran harbor. The container is never even told where it goes.
  assert archive.stat().st_uid == os.getuid()
  tar = next(args for args in _root_runs(harbor_env) if args[-1].startswith("tar -czf"))
  assert str(archive) not in " ".join(tar)


def test_restore_brings_a_data_volume_back(harbor_env):
  assert harbor_env.run("stage", BASIC).returncode == 0
  volume = harbor_env.volumes_root / "data" / BASIC / "config"
  (volume / "db.txt").write_text("v1")
  (volume / "current").symlink_to("db.txt")
  name = _snapshot(harbor_env, BASIC, "one")

  (volume / "db.txt").write_text("v2")
  (volume / "stray.txt").write_text("written since")

  restored = harbor_env.run("restore", BASIC, name, "-y")
  assert restored.returncode == 0, restored.stderr
  assert (volume / "db.txt").read_text() == "v1"
  assert not (volume / "stray.txt").exists()
  assert (volume / "current").is_symlink()
  assert os.readlink(volume / "current") == "db.txt"

  # One container removes the live volume and copies the snapshot's in, so a
  # container that cannot start has deleted nothing.
  put_back = next(
    args for args in _root_runs(harbor_env) if args[-1].startswith("set -e;")
  )
  data_root = harbor_env.volumes_root / "data" / BASIC
  source = harbor_env.root / "snapshots" / BASIC / name / "volumes" / "data" / "config"
  assert _mounts(put_back) == [f"{data_root}:{data_root}", f"{source}:{source}"]
  assert put_back[-1] == f"set -e; rm -rf -- {volume}; cp -a -- {source} {data_root}"


def test_restore_rebuilds_a_removed_app_from_its_snapshot(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "before")

  assert harbor_env.run("rm", app_id, "-y").returncode == 0
  assert not (harbor_env.run_root / app_id).exists()
  assert not harbor_env.app_logtab(app_id).exists()
  assert app_id not in harbor_env.read_db().get("routes", {})

  restored = harbor_env.run("restore", app_id, name, "-y")
  assert restored.returncode == 0, restored.stderr
  assert not (harbor_env.root / "snapshots" / app_id / name).exists()
  assert harbor_env.app_logtab(app_id).is_file()

  # compose.yml is regenerated rather than copied back, so the host ports it
  # publishes are the ones harbordb has just re-allocated.
  compose = yaml.safe_load((harbor_env.run_root / app_id / "compose.yml").read_text())
  assert compose["services"]["main"]["ports"] == ["41000:8080", "9000:80"]
  assert harbor_env.read_db()["routes"][app_id]["web"]["host_port"] == 41000

  ctx = HarborCtx(load_config_file(harbor_env.config))
  assert read_last_app_action(app_id, ctx.config) == f"restored - {name}"

  assert harbor_env.run("start", app_id).returncode == 0


def test_restore_clobbers_whatever_is_there_now(harbor_env):
  """Roll-forward: the snapshot's state simply becomes the current state.

  Nothing about the restored app records that it came from an older point;
  today's config and today's stray files are gone, not set aside.
  """
  app_id = "routes-demo"
  assert harbor_env.run("start", app_id, "--set", "subdomain=photos").returncode == 0
  name = _snapshot(harbor_env, app_id, "photos")

  assert harbor_env.run("config", app_id, "--set", "subdomain=albums").returncode == 0
  scratch = harbor_env.run_root / app_id / "happ" / "scratch.txt"
  scratch.write_text("left over from today")

  restored = harbor_env.run("restore", app_id, name, "-y")
  assert restored.returncode == 0, restored.stderr

  shown = harbor_env.run("config", app_id, "--get", "subdomain")
  assert shown.stdout.strip() == "photos"
  assert not scratch.exists()

  pre = [
    p.name.removesuffix(".tar.gz")
    for p in (harbor_env.root / "snapshots" / app_id).iterdir()
    if p.name.endswith("_pre-restore.tar.gz")
  ]
  assert pre, "restore over a live run dir should take a pre-restore snapshot"


def test_restore_no_snapshot_skips_pre_restore(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "kept")
  before = {p.name for p in (harbor_env.root / "snapshots" / app_id).iterdir()}

  restored = harbor_env.run("restore", app_id, name, "-y", "--no-snapshot")
  assert restored.returncode == 0, restored.stderr

  after = {p.name for p in (harbor_env.root / "snapshots" / app_id).iterdir()}
  assert after == before


def test_restoring_latest_pre_restore_skips_new_snapshot(harbor_env):
  """Undoing the last restore must not mint yet another pre-restore snapshot."""
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "kept")

  restored = harbor_env.run("restore", app_id, name, "-y")
  assert restored.returncode == 0, restored.stderr
  snapshots = harbor_env.root / "snapshots" / app_id
  pre = sorted(
    p.name.removesuffix(".tar.gz")
    for p in snapshots.iterdir()
    if p.name.endswith("_pre-restore.tar.gz")
  )
  assert len(pre) == 1

  undo = harbor_env.run("restore", app_id, pre[-1], "-y")
  assert undo.returncode == 0, undo.stderr
  after = {
    p.name.removesuffix(".tar.gz")
    for p in snapshots.iterdir()
    if p.name.endswith("_pre-restore.tar.gz")
  }
  assert after == set(pre)


def test_restore_refuses_while_containers_are_running(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "up")
  assert harbor_env.run("start", app_id).returncode == 0

  refused = harbor_env.run("restore", app_id, name, "-y")
  assert refused.returncode == 1
  assert f"harbor stop {app_id}" in refused.stderr


def test_restore_names_the_snapshots_it_could_have_used(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "real")

  missing = harbor_env.run("restore", app_id, "nosuchsnapshot", "-y")
  assert missing.returncode == 1
  assert name in missing.stderr

  unknown = harbor_env.run("restore", "never-snapshotted", name, "-y")
  assert unknown.returncode == 1
  assert "No snapshots found" in unknown.stderr


def test_restore_without_snapshot_lists_recent_ones(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "listed")

  omitted = harbor_env.run("restore", app_id)
  assert omitted.returncode == 1
  assert "SNAPSHOT is required" in omitted.stderr
  assert name in omitted.stderr
  assert f"harbor restore {app_id}" in omitted.stderr


def test_restore_without_snapshot_caps_the_list_at_ten(harbor_env):
  """Plant archives directly: real snapshots collide within the same minute."""
  app_id = "ports-demo"
  snap_dir = harbor_env.root / "snapshots" / app_id
  snap_dir.mkdir(parents=True)
  names = [f"2020-01-{day:02d}_00-00Z" for day in range(1, 13)]
  for name in names:
    (snap_dir / f"{name}.tar.gz").write_bytes(b"")

  omitted = harbor_env.run("restore", app_id)
  assert omitted.returncode == 1
  # Newest first, capped at 10; the two oldest stay off the list.
  assert names[0] not in omitted.stderr
  assert names[1] not in omitted.stderr
  assert names[-1] in omitted.stderr
  assert omitted.stderr.index(names[-1]) < omitted.stderr.index(names[2])


def test_restore_declined_at_the_prompt_changes_nothing(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "kept")
  marker = harbor_env.run_root / app_id / "happ" / "marker.txt"
  marker.write_text("still here")

  declined = harbor_env.run("restore", app_id, name, input="n\n")
  assert declined.returncode == 0, declined.stderr
  assert "Nothing restored." in declined.stdout
  assert marker.exists()


def test_snapshot_refuses_while_containers_are_running(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0

  refused = harbor_env.run("snapshot", app_id, "--label", "nope")
  assert refused.returncode == 1
  assert f"harbor stop {app_id}" in refused.stderr


# --- what does and does not need a container -------------------------------


def test_a_volumeless_snapshot_is_cleaned_up_without_a_container(harbor_env):
  """Nothing in it is root-owned, so paying for a container would only make
  cleanup fail whenever docker is down."""
  assert harbor_env.run("stage", "ports-demo").returncode == 0
  taken = harbor_env.run("snapshot", "ports-demo", "--label", "bare")
  assert taken.returncode == 0, taken.stderr

  archive = Path(taken.stdout.split("written to ")[1].strip())
  assert archive.is_file()
  assert not (archive.parent / archive.name.removesuffix(".tar.gz")).exists()
  # The archive itself still needs one; removing the staging folder does not.
  assert not [args for args in _root_runs(harbor_env) if "rm -rf -- " in args[-1]]


def test_a_failed_extract_reports_itself_not_its_cleanup(tmp_path, monkeypatch):
  """The cleanup runs in the same container that just failed, so it fails too.
  Its message must not replace the only one that says what went wrong."""
  snapshot_mod = importlib.import_module("harbor.lib.lifecycle.snapshot")
  archive = tmp_path / "2026-01-01_00-00Z.tar.gz"
  archive.write_bytes(b"not a tarball")

  def fail(what: str, script: str, mounts, *, stdout=None) -> None:
    if what.startswith("extract"):
      # tar got far enough to make the directory, then died.
      (archive.parent / "2026-01-01_00-00Z").mkdir()
      (archive.parent / "2026-01-01_00-00Z" / "volumes").mkdir()
      (archive.parent / "2026-01-01_00-00Z" / "volumes" / "data").mkdir()
    raise RuntimeError(f"Unable to {what}. That container exited 2.")

  monkeypatch.setattr(snapshot_mod, "run_as_root", fail)
  with pytest.raises(RuntimeError) as raised:
    snapshot_mod.extract_snapshot(archive)
  assert "extract the snapshot" in str(raised.value)
  assert "Unable to remove" not in str(raised.value)


def test_a_colon_in_a_path_is_refused(tmp_path):
  """`-v host:guest` is colon-delimited, so a colon would silently bind
  something other than what was asked for."""
  odd = tmp_path / "we:ird"
  odd.mkdir()
  with pytest.raises(ValueError) as raised:
    run_as_root("do the thing", "true", [odd])
  assert "colon" in str(raised.value)
