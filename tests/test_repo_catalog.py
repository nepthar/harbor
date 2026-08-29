"""Extra repos: `[[repo]]` directories beyond `repos/main`.

Several repos can carry the same app id. Harbor never picks between them:
`bundle_path` refuses, `doctor` reports, and `<app>@<repo>` or a full path is
how you say which one you mean.
"""

import logging
from pathlib import Path

import pytest

from harbor.lib.config import load_config_file
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import bound_to

MANIFEST = """\
[app]
version      = "0.1.0"
display_name = "{display}"

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "echo hi"]
restart = "no"
"""


def add_repo_block(harbor_env, name: str, path: Path) -> None:
  with open(harbor_env.config, "a") as f:
    f.write(f'\n[[repo]]\nname = "{name}"\npath = "{path}"\n')


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


def test_main_is_always_the_first_repo(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  config = load_config_file(harbor_env.config)

  assert list(config.repos) == ["main", "hrbr-dev"]
  assert config.repos["main"].path == harbor_env.root / "repos" / "main"
  assert config.repos["hrbr-dev"].path == dev


def test_the_default_config_has_only_main(harbor_env):
  config = load_config_file(harbor_env.config)
  assert list(config.repos) == ["main"]


@pytest.mark.parametrize(
  ("block", "problem"),
  [
    ('[[repo]]\nname = "main"\npath = "elsewhere"\n', "defined twice"),
    ('[[repo]]\nname = "dev"\npath = "repos/main"\n', "already the 'main' repo"),
    ('[[repo]]\npath = "elsewhere"\n', "needs a name and one of path or url"),
    ('[[repo]]\nname = "dev"\n', "exactly one of path"),
    (
      '[[repo]]\nname = "dev"\npath = "d"\nurl = "github://a/b/main"\n',
      "exactly one of path",
    ),
    ('[[repo]]\nname = "b a d"\npath = "elsewhere"\n', "not a valid name"),
  ],
)
def test_a_bad_repo_is_reported_and_ignored(harbor_env, caplog, block, problem):
  """A typo in an optional section must not stop every harbor command."""
  with open(harbor_env.config, "a") as f:
    f.write("\n" + block)

  with caplog.at_level(logging.ERROR, logger="harbor.config"):
    config = load_config_file(harbor_env.config)

  assert list(config.repos) == ["main"]
  assert problem in caplog.text
  assert "Ignoring every [[repo]]" in caplog.text


def test_one_bad_repo_drops_the_good_ones_too(harbor_env, caplog):
  """All or nothing: a half-applied catalog is harder to explain than none."""
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_repo_block(harbor_env, "hrbr-dev", dev)
  add_repo_block(harbor_env, "b a d", harbor_env.root / "other")

  with caplog.at_level(logging.ERROR, logger="harbor.config"):
    config = load_config_file(harbor_env.config)

  assert list(config.repos) == ["main"]
  assert "not a valid name" in caplog.text


def test_a_bad_repo_still_lets_commands_run(harbor_env):
  add_repo_block(harbor_env, "b a d", harbor_env.root / "other")

  result = harbor_env.run("catalog")

  assert result.returncode == 0, result.stderr
  assert "ports-demo" in result.stdout


def test_two_extra_repos_may_not_share_a_name(harbor_env, caplog):
  add_repo_block(harbor_env, "dev", harbor_env.root / "one")
  add_repo_block(harbor_env, "dev", harbor_env.root / "two")

  with caplog.at_level(logging.ERROR, logger="harbor.config"):
    config = load_config_file(harbor_env.config)

  assert list(config.repos) == ["main"]
  assert "defined twice" in caplog.text


# --- using an extra source --------------------------------------------------


def test_an_app_in_a_second_source_stages_by_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("install", "dev-app")

  assert result.returncode == 0, result.stderr
  assert (harbor_env.run_root / "dev-app" / "compose.yml").is_file()
  # Staged from where it lives. Nothing is copied or linked into apps/.
  assert not (harbor_env.root / "repos" / "main" / "dev-app.happ").exists()


def test_catalog_names_the_source_of_every_app(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("catalog")

  assert result.returncode == 0, result.stderr
  assert result.stdout.splitlines()[0].split() == ["APP_ID", "SOURCE", "STATUS", "PATH"]
  assert _row(result.stdout, "dev-app") == [
    "dev-app",
    "hrbr-dev",
    "-",
    str(dev / "dev-app.happ"),
  ]
  assert _row(result.stdout, "ports-demo")[1] == "main"


def test_catalog_reports_the_last_action_as_status(harbor_env):
  assert harbor_env.run("install", "ports-demo").returncode == 0

  staged = _row(harbor_env.run("catalog").stdout, "ports-demo")
  assert staged[2] == "installed"

  assert harbor_env.run("start", "ports-demo").returncode == 0
  started = _row(harbor_env.run("catalog").stdout, "ports-demo")
  assert started[2] == "started"

  # Not installed, so no status of its own.
  assert _row(harbor_env.run("catalog").stdout, "routes-demo")[2] == "-"


def test_an_ambiguous_id_gets_a_row_per_source(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  rows = _rows(harbor_env.run("catalog").stdout, "ports-demo")

  assert [row[1] for row in rows] == ["main", "hrbr-dev"]


def test_status_follows_the_bundle_that_is_actually_installed(harbor_env):
  """Two bundles share the id; only the one staged from carries its status."""
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  assert harbor_env.run("start", str(bundle)).returncode == 0

  rows = _rows(harbor_env.run("catalog").stdout, "ports-demo")
  assert [(row[1], row[2]) for row in rows] == [
    ("main", "-"),
    ("hrbr-dev", "started"),
  ]

  # Re-staging from the other bundle is a rebinding: refused on its own, and
  # the status stays where it was.
  assert harbor_env.run("stop", "ports-demo").returncode == 0
  from_main = harbor_env.root / "repos" / "main" / "ports-demo.happ"
  refused = harbor_env.run("install", str(from_main))
  assert refused.returncode == 1
  assert "previously installed from" in refused.stderr

  assert harbor_env.run("install", str(from_main), "--force").returncode == 0

  rows = _rows(harbor_env.run("catalog").stdout, "ports-demo")
  assert [(row[1], row[2]) for row in rows] == [
    ("main", "installed"),
    ("hrbr-dev", "-"),
  ]


def test_a_bundle_reachable_through_two_repos_counts_twice(harbor_env):
  """Two entries are two catalog rows, even when they name one directory.

  A checkout can be a repo of its own and be linked into another, so the id is
  ambiguous like any other. Installing by path resolves the link, though, so
  both routes to the bundle are one binding rather than a rebinding, and the
  status lands on the bundle actually staged.
  """
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "dev-app")
  add_repo_block(harbor_env, "hrbr-dev", dev)
  link = harbor_env.root / "repos" / "main" / "dev-app.happ"
  link.symlink_to(bundle)

  entries = ctx_for(harbor_env).app_catalog()["dev-app"]
  assert [entry.source for entry in entries] == ["main", "hrbr-dev"]

  by_id = harbor_env.run("install", "dev-app")
  assert by_id.returncode == 1
  assert "More than one repo carries" in by_id.stderr

  assert harbor_env.run("install", str(link)).returncode == 0
  # Naming the link and naming its target are the same binding, so neither
  # install is refused as a change of source.
  assert harbor_env.run("install", str(bundle)).returncode == 0

  rows = _rows(harbor_env.run("catalog").stdout, "dev-app")
  assert [(row[1], row[2]) for row in rows] == [
    ("main", "-"),
    ("hrbr-dev", "installed"),
  ]


def test_doctor_reports_a_missing_repo_directory(harbor_env):
  add_repo_block(harbor_env, "gone", harbor_env.root / "not-here")

  result = harbor_env.run("doctor")

  assert result.returncode == 1
  assert "is not a directory" in result.stderr


# --- one id in two sources --------------------------------------------------


def test_an_id_in_two_sources_cannot_be_staged_or_started_by_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo")  # apps/ports-demo.happ is a fixture happ
  add_repo_block(harbor_env, "hrbr-dev", dev)

  staged = harbor_env.run("install", "ports-demo")
  assert staged.returncode == 1
  assert "More than one repo carries" in staged.stderr
  assert str(dev) in staged.stderr

  started = harbor_env.run("start", "ports-demo")
  assert started.returncode == 1
  assert "More than one repo carries" in started.stderr


def test_doctor_reports_an_ambiguous_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("doctor")

  assert result.returncode == 1
  assert "More than one repo carries" in result.stderr
  assert "hrbr-dev" in result.stderr


def test_a_full_path_picks_which_source_to_stage(harbor_env):
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  result = harbor_env.run("install", str(bundle))

  assert result.returncode == 0, result.stderr
  staged = harbor_env.run_root / "ports-demo" / "happ" / "manifest.toml"
  assert "From dev" in staged.read_text()
  # Picking one did not add a third entry for the id.
  assert len(ctx_for(harbor_env).app_catalog()["ports-demo"]) == 2


def test_only_one_app_is_staged_per_id(harbor_env):
  """Two bundles, one run dir: staging the other replaces what is installed."""
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  assert harbor_env.run("install", str(bundle)).returncode == 0
  staged = harbor_env.run_root / "ports-demo" / "happ" / "manifest.toml"
  assert "From dev" in staged.read_text()

  from_main = harbor_env.root / "repos" / "main" / "ports-demo.happ"
  assert harbor_env.run("install", str(from_main), "--force").returncode == 0

  assert "From dev" not in staged.read_text()
  assert [p.name for p in harbor_env.run_root.iterdir()] == ["ports-demo"]


def test_an_ambiguous_id_still_stops_and_removes(harbor_env):
  """The staged copy is unambiguous, so lifecycle commands keep working."""
  dev = harbor_env.root / "dev-apps"
  bundle = a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)
  assert harbor_env.run("start", str(bundle)).returncode == 0

  assert harbor_env.run("stop", "ports-demo").returncode == 0
  assert harbor_env.run("rm", "ports-demo", "-y").returncode == 0


# --- binding ----------------------------------------------------------------


def test_installing_records_the_repo_it_came_from(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "dev-app")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  assert harbor_env.run("install", "dev-app").returncode == 0

  assert bound_to("dev-app", ctx_for(harbor_env)) == "repo hrbr-dev"


def test_a_repo_can_be_named_to_settle_an_ambiguous_id(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)

  assert harbor_env.run("install", "ports-demo@hrbr-dev").returncode == 0

  staged = harbor_env.run_root / "ports-demo" / "happ" / "manifest.toml"
  assert "From dev" in staged.read_text()
  assert bound_to("ports-demo", ctx_for(harbor_env)) == "repo hrbr-dev"


def test_naming_a_repo_that_does_not_carry_the_app_says_which_do(harbor_env):
  result = harbor_env.run("install", "ports-demo@nowhere")

  assert result.returncode == 1
  assert "does not carry" in result.stderr
  assert "main" in result.stderr


def test_a_binding_outlives_an_uninstall(harbor_env):
  """Config and secrets survive an uninstall, so the source they were made for
  has to survive with them."""
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)
  assert harbor_env.run("install", "ports-demo@hrbr-dev").returncode == 0
  assert harbor_env.run("uninstall", "ports-demo", "-y").returncode == 0

  refused = harbor_env.run("install", "ports-demo@main")
  assert refused.returncode == 1
  assert "previously installed from repo hrbr-dev" in refused.stderr
  assert "--force" in refused.stderr
  assert "uninstall --purge ports-demo" in refused.stderr


def test_a_purge_clears_the_binding(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)
  assert harbor_env.run("install", "ports-demo@hrbr-dev").returncode == 0
  assert harbor_env.run("uninstall", "--purge", "ports-demo", "-y").returncode == 0

  assert harbor_env.run("install", "ports-demo@main").returncode == 0
  assert bound_to("ports-demo", ctx_for(harbor_env)) == "repo main"


def test_force_installs_over_a_different_source(harbor_env):
  dev = harbor_env.root / "dev-apps"
  a_happ(dev, "ports-demo", display="From dev")
  add_repo_block(harbor_env, "hrbr-dev", dev)
  assert harbor_env.run("install", "ports-demo@hrbr-dev").returncode == 0

  assert harbor_env.run("install", "ports-demo@main", "--force").returncode == 0
  assert bound_to("ports-demo", ctx_for(harbor_env)) == "repo main"


def test_reinstalling_from_the_same_repo_is_not_a_rebinding(harbor_env):
  assert harbor_env.run("install", "ports-demo").returncode == 0
  assert harbor_env.run("install", "ports-demo").returncode == 0
