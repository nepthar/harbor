"""Extra app sources: `[[app_source]]` directories beyond `apps/`.

Several sources can carry the same app id. Harbor never picks between them:
`bundle_path` refuses, `doctor` reports, and a full path is how you say which
one you mean.
"""

import logging
from pathlib import Path

import pytest

from harbor.lib import fetch as fetch_lib
from harbor.lib.config import load_config_file
from harbor.lib.harbor import HarborCtx

MANIFEST = """\
[app]
version      = "0.1.0"
display_name = "{display}"

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "echo hi"]
restart = "no"
"""


def add_source(harbor_env, name: str, location: Path) -> None:
  with open(harbor_env.config, "a") as f:
    f.write(f'\n[[app_source]]\nname = "{name}"\nlocation = "{location}"\n')


def a_happ(parent: Path, app_id: str, display: str = "Extra") -> Path:
  parent.mkdir(parents=True, exist_ok=True)
  bundle = parent / f"{app_id}.happ"
  bundle.mkdir()
  (bundle / "manifest.toml").write_text(MANIFEST.format(display=display))
  return bundle


def ctx_for(harbor_env) -> HarborCtx:
  return HarborCtx(load_config_file(harbor_env.config))


def _rows(catalog_output: str, app_id: str) -> list[list[str]]:
  """Every `harbor catalog` row for `app_id`, each split into its columns."""
  return [
    line.split()
    for line in catalog_output.splitlines()
    if line.startswith(app_id + " ")
  ]


def _row(catalog_output: str, app_id: str) -> list[str]:
  """The one `harbor catalog` row for `app_id`, split into its columns."""
  rows = _rows(catalog_output, app_id)
  assert len(rows) == 1, f"expected one {app_id} row, got {rows}"
  return rows[0]


# --- configuration ----------------------------------------------------------


def test_apps_is_always_the_first_source(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_source(harbor_env, "hrbr-dev", dev)

  config = load_config_file(harbor_env.config)

  assert list(config.app_sources) == ["apps", "hrbr-dev"]
  assert config.app_sources["apps"] == harbor_env.root / "apps"
  assert config.app_sources["hrbr-dev"] == dev


def test_the_default_config_has_only_the_apps_source(harbor_env):
  config = load_config_file(harbor_env.config)
  assert list(config.app_sources) == ["apps"]


@pytest.mark.parametrize(
  ("block", "problem"),
  [
    ('[[app_source]]\nname = "apps"\nlocation = "elsewhere"\n', "defined twice"),
    ('[[app_source]]\nname = "dev"\nlocation = "apps"\n', "already the 'apps' source"),
    ('[[app_source]]\nlocation = "elsewhere"\n', "needs a name and a location"),
    ('[[app_source]]\nname = "dev"\n', "needs a name and a location"),
    ('[[app_source]]\nname = "b a d"\nlocation = "elsewhere"\n', "not a valid name"),
  ],
)
def test_a_bad_app_source_is_reported_and_ignored(harbor_env, caplog, block, problem):
  """A typo in an optional section must not stop every harbor command."""
  with open(harbor_env.config, "a") as f:
    f.write("\n" + block)

  with caplog.at_level(logging.ERROR, logger="harbor.config"):
    config = load_config_file(harbor_env.config)

  assert list(config.app_sources) == ["apps"]
  assert problem in caplog.text
  assert "Ignoring every [[app_source]]" in caplog.text


def test_one_bad_source_drops_the_good_ones_too(harbor_env, caplog):
  """All or nothing: a half-applied catalog is harder to explain than none."""
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_source(harbor_env, "hrbr-dev", dev)
  add_source(harbor_env, "b a d", harbor_env.root / "other")

  with caplog.at_level(logging.ERROR, logger="harbor.config"):
    config = load_config_file(harbor_env.config)

  assert list(config.app_sources) == ["apps"]
  assert "not a valid name" in caplog.text


def test_a_bad_app_source_still_lets_commands_run(harbor_env):
  add_source(harbor_env, "b a d", harbor_env.root / "other")

  result = harbor_env.run("catalog")

  assert result.returncode == 0, result.stderr
  assert "ports-demo" in result.stdout


def test_two_extra_sources_may_not_share_a_name(harbor_env, caplog):
  add_source(harbor_env, "dev", harbor_env.root / "one")
  add_source(harbor_env, "dev", harbor_env.root / "two")

  with caplog.at_level(logging.ERROR, logger="harbor.config"):
    config = load_config_file(harbor_env.config)

  assert list(config.app_sources) == ["apps"]
  assert "defined twice" in caplog.text


# --- using an extra source --------------------------------------------------


def test_an_app_in_a_second_source_stages_by_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_source(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("stage", "dev-app")

  assert result.returncode == 0, result.stderr
  assert (harbor_env.run_root / "dev-app" / "compose.yml").is_file()
  # Staged from where it lives. Nothing is copied or linked into apps/.
  assert not (harbor_env.root / "apps" / "dev-app.happ").exists()


def test_catalog_names_the_source_of_every_app(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_source(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("catalog")

  assert result.returncode == 0, result.stderr
  assert result.stdout.splitlines()[0].split() == ["APP_ID", "SOURCE", "STATUS", "PATH"]
  assert _row(result.stdout, "dev-app") == [
    "dev-app",
    "hrbr-dev",
    "-",
    str(dev / "dev-app.happ"),
  ]
  assert _row(result.stdout, "ports-demo")[1] == "apps"


def test_catalog_reports_the_last_action_as_status(harbor_env):
  assert harbor_env.run("stage", "ports-demo").returncode == 0

  staged = _row(harbor_env.run("catalog").stdout, "ports-demo")
  assert staged[2] == "staged"

  assert harbor_env.run("start", "ports-demo").returncode == 0
  started = _row(harbor_env.run("catalog").stdout, "ports-demo")
  assert started[2] == "started"

  # Not installed, so no status of its own.
  assert _row(harbor_env.run("catalog").stdout, "routes-demo")[2] == "-"


def test_an_ambiguous_id_gets_a_row_per_source(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo")
  add_source(harbor_env, "hrbr-dev", dev)

  rows = _rows(harbor_env.run("catalog").stdout, "ports-demo")

  assert [row[1] for row in rows] == ["apps", "hrbr-dev"]


def test_status_follows_the_bundle_that_is_actually_installed(harbor_env):
  """Two bundles share the id; only the one staged from carries its status."""
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_source(harbor_env, "hrbr-dev", dev)

  assert harbor_env.run("start", str(bundle)).returncode == 0

  rows = _rows(harbor_env.run("catalog").stdout, "ports-demo")
  assert [(row[1], row[2]) for row in rows] == [
    ("apps", "-"),
    ("hrbr-dev", "started"),
  ]

  # Re-stage from the other source and the status moves with it.
  assert harbor_env.run("stop", "ports-demo").returncode == 0
  from_apps = harbor_env.root / "apps" / "ports-demo.happ"
  assert harbor_env.run("stage", str(from_apps)).returncode == 0

  rows = _rows(harbor_env.run("catalog").stdout, "ports-demo")
  assert [(row[1], row[2]) for row in rows] == [
    ("apps", "staged"),
    ("hrbr-dev", "-"),
  ]


def test_a_bundle_symlinked_into_two_sources_counts_twice(harbor_env):
  """Two entries are two apps, even when they name one directory.

  `stage <path>` leaves a symlink in apps/, and that path is often inside a
  checkout that is itself a source. Harbor does not resolve them back
  together: the entries differ, so the id is ambiguous like any other, and
  only the entry `stage` recorded carries the status.
  """
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "dev-app")
  add_source(harbor_env, "hrbr-dev", dev)
  link = harbor_env.root / "apps" / "dev-app.happ"
  link.symlink_to(bundle)

  entries = ctx_for(harbor_env).app_catalog()["dev-app"]
  assert [entry.source for entry in entries] == ["apps", "hrbr-dev"]

  by_id = harbor_env.run("stage", "dev-app")
  assert by_id.returncode == 1
  assert "Multiple apps matched" in by_id.stderr

  assert harbor_env.run("stage", str(link)).returncode == 0
  rows = _rows(harbor_env.run("catalog").stdout, "dev-app")
  assert [(row[1], row[2]) for row in rows] == [("apps", "staged"), ("hrbr-dev", "-")]


def test_doctor_reports_a_missing_app_source_directory(harbor_env):
  add_source(harbor_env, "gone", harbor_env.root / "not-here")

  result = harbor_env.run("doctor")

  assert result.returncode == 1
  assert "is not a directory" in result.stderr


# --- one id in two sources --------------------------------------------------


def test_an_id_in_two_sources_cannot_be_staged_or_started_by_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo")  # apps/ports-demo.happ is a fixture happ
  add_source(harbor_env, "hrbr-dev", dev)

  staged = harbor_env.run("stage", "ports-demo")
  assert staged.returncode == 1
  assert "Multiple apps matched" in staged.stderr
  assert str(dev) in staged.stderr

  started = harbor_env.run("start", "ports-demo")
  assert started.returncode == 1
  assert "Multiple apps matched" in started.stderr


def test_doctor_reports_an_ambiguous_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo")
  add_source(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("doctor")

  assert result.returncode == 1
  assert "Multiple apps matched" in result.stderr
  assert "hrbr-dev" in result.stderr


def test_a_full_path_picks_which_source_to_stage(harbor_env):
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_source(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("stage", str(bundle))

  assert result.returncode == 0, result.stderr
  staged = harbor_env.run_root / "ports-demo" / "happ" / "manifest.toml"
  assert "From dev" in staged.read_text()
  # Picking one did not add a third entry for the id.
  assert len(ctx_for(harbor_env).app_catalog()["ports-demo"]) == 2


def test_only_one_app_is_staged_per_id(harbor_env):
  """Two bundles, one run dir: staging the other replaces what is installed."""
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_source(harbor_env, "hrbr-dev", dev)

  assert harbor_env.run("stage", str(bundle)).returncode == 0
  staged = harbor_env.run_root / "ports-demo" / "happ" / "manifest.toml"
  assert "From dev" in staged.read_text()

  from_apps = harbor_env.root / "apps" / "ports-demo.happ"
  assert harbor_env.run("stage", str(from_apps)).returncode == 0

  assert "From dev" not in staged.read_text()
  assert [p.name for p in harbor_env.run_root.iterdir()] == ["ports-demo"]


def test_an_ambiguous_id_still_stops_and_removes(harbor_env):
  """The staged copy is unambiguous, so lifecycle commands keep working."""
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_source(harbor_env, "hrbr-dev", dev)
  assert harbor_env.run("start", str(bundle)).returncode == 0

  assert harbor_env.run("stop", "ports-demo").returncode == 0
  assert harbor_env.run("rm", "ports-demo", "-y").returncode == 0


def test_fetch_refuses_an_id_another_source_carries(harbor_env, monkeypatch):
  # Unreachable: the collision is caught before any request is made, and this
  # keeps a regression from reaching the real GitHub.
  monkeypatch.setattr(fetch_lib, "API_ROOT", "http://127.0.0.1:1/api")
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "hello-world")
  add_source(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("fetch", "github:nepthar/harbor/main/apps/hello-world.happ")

  assert result.returncode == 1
  assert "already in the hrbr-dev app source" in result.stderr
