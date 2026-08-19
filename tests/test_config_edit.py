"""Editing config.toml: what survives, what is refused, and what is never left
half-written."""

from __future__ import annotations

import json

import pytest

from harbor.lib.config import load_config
from harbor.lib.config_edit import (
  add_host_volume,
  edit_config,
  remove_host_volume,
  set_host_volume,
)
from harbor.lib.harbor import LOCK_KEY, HarborCtx
from harbor.lib.logtab import LogTab


def ctx_of(harbor_env) -> HarborCtx:
  """A context reading the config as it is on disk right now."""
  config = load_config()
  assert config is not None
  return HarborCtx(config)


@pytest.fixture
def host_dir(harbor_env):
  """A directory no fixture host_volume already points at."""
  path = harbor_env.root / "extra-data"
  path.mkdir()
  return path


def test_an_edit_keeps_the_comments_around_it(harbor_env, host_dir):
  original = harbor_env.config.read_text()
  harbor_env.config.write_text(
    original + "\n# a note the operator left\n# [[app_source]]\n# name = 'dev'\n"
  )

  add_host_volume(ctx_of(harbor_env), "extra", str(host_dir))

  text = harbor_env.config.read_text()
  assert "# a note the operator left" in text
  assert "# [[app_source]]" in text
  # Everything that was there before is still there, in order.
  assert text.startswith(original.rstrip() + "\n" or original)
  assert "[host_volume.media]" in text


def test_add_set_and_remove_round_trip(harbor_env, host_dir):
  other = harbor_env.root / "other-data"
  other.mkdir()

  add_host_volume(ctx_of(harbor_env), "extra", str(host_dir), readonly=True)
  volume = ctx_of(harbor_env).config.host_volumes["extra"]
  assert volume.path == host_dir
  assert volume.readonly is True

  # Flags are the whole truth on --set, not a patch: readonly clears.
  set_host_volume(ctx_of(harbor_env), "extra", str(other))
  volume = ctx_of(harbor_env).config.host_volumes["extra"]
  assert volume.path == other
  assert volume.readonly is False

  remove_host_volume(ctx_of(harbor_env), "extra")
  assert "extra" not in ctx_of(harbor_env).config.host_volumes
  assert "[host_volume.extra]" not in harbor_env.config.read_text()


def test_a_harbor_root_placeholder_resolves(harbor_env):
  (harbor_env.root / "shared").mkdir()
  add_host_volume(ctx_of(harbor_env), "shared", "${harbor_root}/shared")
  assert ctx_of(harbor_env).config.host_volumes["shared"].path == (
    harbor_env.root / "shared"
  )


def test_config_toml_is_never_left_invalid(harbor_env):
  """The whole reason edits go through the real loader before landing."""
  before = harbor_env.config.read_text()

  with pytest.raises(ValueError) as raised:
    with edit_config(ctx_of(harbor_env), "a bad idea") as document:
      document["port_base"] = "not a number"

  assert "not valid" in str(raised.value)
  assert harbor_env.config.read_text() == before
  # And no half-written sibling left behind for the next command to trip on.
  assert not list(harbor_env.root.glob(".config.toml*"))


def test_an_edit_is_recorded_as_a_lock_holder(harbor_env, host_dir):
  """config.toml is harbor-wide state; two writers would lose an edit."""
  add_host_volume(ctx_of(harbor_env), "extra", str(host_dir))

  record = json.loads(
    LogTab(ctx_of(harbor_env).config.activity_log).read(LOCK_KEY).value
  )
  assert record["by"] == "host-volume add extra"
  assert record["state"] == "released"


@pytest.mark.parametrize(
  ("tag", "path", "expected"),
  [
    ("extra", "/no/such/directory", "No such directory"),
    ("bad tag", None, "Invalid identifier"),
  ],
)
def test_add_refuses_and_says_why(harbor_env, host_dir, tag, path, expected):
  with pytest.raises(ValueError) as raised:
    add_host_volume(ctx_of(harbor_env), tag, path or str(host_dir))
  assert expected in str(raised.value)


def test_a_duplicate_tag_names_the_command_that_changes_it(harbor_env, host_dir):
  add_host_volume(ctx_of(harbor_env), "extra", str(host_dir))
  with pytest.raises(ValueError) as raised:
    add_host_volume(ctx_of(harbor_env), "extra", str(host_dir))
  assert "already exists" in str(raised.value)
  assert "--set extra=" in str(raised.value)


def test_changing_an_unknown_tag_lists_the_known_ones(harbor_env, host_dir):
  add_host_volume(ctx_of(harbor_env), "extra", str(host_dir))
  for operation in (
    lambda: set_host_volume(ctx_of(harbor_env), "nope", str(host_dir)),
    lambda: remove_host_volume(ctx_of(harbor_env), "nope"),
  ):
    with pytest.raises(ValueError) as raised:
      operation()
    assert "known tags:" in str(raised.value)
    assert "extra" in str(raised.value)


def test_a_new_host_volume_is_immediately_bindable(harbor_env, host_dir):
  """The point of the feature: declare it, then bind an app to it."""
  add_host_volume(ctx_of(harbor_env), "fresh", str(host_dir))
  bound = harbor_env.run("config", "host-volumes", "--bind", "hostvol1=fresh")
  assert bound.returncode == 0, bound.stderr
  assert harbor_env.run("start", "host-volumes").returncode == 0
  link = harbor_env.run_root / "host-volumes" / "volumes" / "host" / "hostvol1"
  assert link.resolve() == host_dir
