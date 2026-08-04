"""Snapshot and restore, over an app that declares no volumes.

That restriction is the point: copying data volumes needs `sudo cp -a`, because
containers write as root. Everything up to that copy -- refusals, the config
round trip, compose regeneration -- is reachable without it. The copy itself
is in docs/testing.md as a live test.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from harbor.lib.apps import read_last_app_action
from harbor.lib.config import load_config_file
from harbor.lib.harbor import HarborCtx


def _snapshot(harbor_env, app_id: str, label: str) -> str:
  """Stop `app_id`, snapshot it, and return the snapshot folder name."""
  assert harbor_env.run("stop", app_id).returncode == 0
  taken = harbor_env.run("snapshot", app_id, "--label", label)
  assert taken.returncode == 0, taken.stderr
  return Path(taken.stdout.split("written to ")[1].strip()).name


def test_restore_rebuilds_a_removed_app_from_its_snapshot(harbor_env):
  app_id = "ports-demo"
  assert harbor_env.run("start", app_id).returncode == 0
  name = _snapshot(harbor_env, app_id, "before")

  assert harbor_env.run("rm", app_id, "-y").returncode == 0
  assert not (harbor_env.run_root / app_id).exists()
  assert app_id not in harbor_env.read_db().get("routes", {})

  restored = harbor_env.run("restore", app_id, name, "-y")
  assert restored.returncode == 0, restored.stderr

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
    p.name
    for p in (harbor_env.root / "snapshots" / app_id).iterdir()
    if p.name.endswith("_pre-restore")
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
