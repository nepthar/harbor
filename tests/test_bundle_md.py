"""Single-file `.klso.md` bundles.

`load_bundle` parses the markdown into files; staging extracts them so the run
tree is a plain directory and everything downstream of `stage` is unchanged.
"""

from pathlib import Path

import pytest

from kelso.lib.bundle import BundleFolder, BundleMdFile, load_bundle, scan_bundles

MD_APP = """\
# A tiny bundle, as one auditable file.

```toml klso_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Markdown demo"
description  = "A bundle distributed as a single markdown file"

[volumes]
hello = { kind = "app", src = "bin/hello.sh" }

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "/app/hello.sh"]
volumes = { hello = "/app/hello.sh" }
restart = "no"
```

```bash klso_path="bin/hello.sh:+x"
echo "hello from markdown"
```
"""


def write_md_bundle(parent: Path, app_id: str = "md-demo", body: str = MD_APP) -> Path:
  parent.mkdir(parents=True, exist_ok=True)
  path = parent / f"{app_id}.klso.md"
  path.write_text(body)
  return path


def test_load_bundle_md_parses_files(tmp_path: Path):
  bundle = load_bundle(write_md_bundle(tmp_path))
  assert isinstance(bundle, BundleMdFile)
  assert str(bundle.app_id) == "md-demo"
  assert sorted(str(p) for p in bundle.files()) == ["bin/hello.sh", "manifest.toml"]


def test_load_bundle_folder_still_loads(tmp_path: Path):
  folder = tmp_path / "plain.klso"
  folder.mkdir()
  (folder / "manifest.toml").write_text("[app]\nversion = '0.1.0'\n")
  bundle = load_bundle(folder)
  assert isinstance(bundle, BundleFolder)
  assert str(bundle.app_id) == "plain"


def test_md_extract_writes_files_and_exec_bit(tmp_path: Path):
  bundle = load_bundle(write_md_bundle(tmp_path))
  target = tmp_path / "out"
  bundle.extract_to(target)

  script = target / "bin" / "hello.sh"
  assert (target / "manifest.toml").is_file()
  assert script.is_file()
  assert script.stat().st_mode & 0o111
  assert not (target / "manifest.toml").stat().st_mode & 0o111
  assert "hello from markdown" in script.read_text()


def test_md_app_stack_builds_from_embedded_manifest(tmp_path: Path):
  stack = load_bundle(write_md_bundle(tmp_path)).app_stack()
  assert str(stack.app) == "md-demo"
  assert list(stack.run_units) == ["main"]
  assert stack.volumes["hello"].kind == "app"


@pytest.mark.parametrize(
  ("body", "problem"),
  [
    ("no code blocks here\n", "does not contain any files"),
    ('```sh klso_path="run.sh"\necho hi\n```\n', "missing a manifest.toml"),
    ('```toml klso_path="manifest.toml"\n[app]\n', "unclosed file block"),
    (
      '```toml klso_path="manifest.toml"\nx = 1\n```\n'
      '```sh klso_path="../escape.sh"\nboom\n```\n',
      "traverse up",
    ),
    (
      '```toml klso_path="manifest.toml"\nx = 1\n```\n'
      '```sh klso_path="/etc/passwd"\nboom\n```\n',
      "absolute file paths",
    ),
  ],
)
def test_load_bundle_md_rejects_bad_documents(tmp_path: Path, body: str, problem: str):
  path = write_md_bundle(tmp_path, body=body)
  with pytest.raises(ValueError, match=problem):
    load_bundle(path)


def test_scan_bundles_finds_both_flavors_and_skips_the_rest(tmp_path: Path):
  write_md_bundle(tmp_path)
  folder = tmp_path / "plain.klso"
  folder.mkdir()
  (folder / "manifest.toml").write_text("[app]\nversion = '0.1.0'\n")
  (tmp_path / "no-manifest.klso").mkdir()
  (tmp_path / "README.md").write_text("not a bundle\n")

  found = dict(scan_bundles(tmp_path))

  assert found == {"md-demo": Path("md-demo.klso.md"), "plain": Path("plain.klso")}
  assert dict(scan_bundles(tmp_path / "does-not-exist")) == {}


def test_scan_bundles_prefers_the_folder_flavor(tmp_path: Path):
  write_md_bundle(tmp_path, app_id="both")
  folder = tmp_path / "both.klso"
  folder.mkdir()
  (folder / "manifest.toml").write_text("[app]\nversion = '0.1.0'\n")

  first = {}
  for app_id, rel_path in scan_bundles(tmp_path):
    first.setdefault(app_id, rel_path)

  assert first == {"both": Path("both.klso")}


def test_stage_md_bundle_from_catalog(kelso_env):
  write_md_bundle(kelso_env.main_repo)

  result = kelso_env.run("install", "md-demo")
  assert result.returncode == 0, result.stderr

  bundle_dir = kelso_env.run_root / "md-demo" / "bundle"
  assert (bundle_dir / "manifest.toml").is_file()
  assert (bundle_dir / "bin" / "hello.sh").stat().st_mode & 0o111


def test_stage_md_bundle_by_path_adds_nothing_to_a_repo(kelso_env):
  source = write_md_bundle(kelso_env.root / "elsewhere")

  result = kelso_env.run("install", str(source))
  assert result.returncode == 0, result.stderr

  assert not (kelso_env.main_repo / "md-demo.klso.md").exists()
  assert (kelso_env.run_root / "md-demo" / "bundle" / "manifest.toml").is_file()


def test_two_flavors_of_one_id_make_it_ambiguous(kelso_env):
  """Both flavors in one repo is the same ambiguity as two repos."""
  # ports-demo.klso (a fixture directory) already owns this id.
  write_md_bundle(kelso_env.main_repo, app_id="ports-demo")

  by_id = kelso_env.run("install", "ports-demo")
  assert by_id.returncode == 1
  assert "More than one repo carries" in by_id.stderr

  doctor = kelso_env.run("doctor")
  assert doctor.returncode == 1
  assert "More than one repo carries" in doctor.stderr


def test_a_full_path_picks_the_flavor_to_stage(kelso_env):
  write_md_bundle(kelso_env.main_repo, app_id="ports-demo")
  md = kelso_env.main_repo / "ports-demo.klso.md"

  result = kelso_env.run("install", str(md))
  assert result.returncode == 0, result.stderr
  # The md flavor's manifest, not the fixture directory's.
  staged = kelso_env.run_root / "ports-demo" / "bundle" / "manifest.toml"
  assert "Markdown demo" in staged.read_text()
  # Nothing new in apps/: it was already catalogued where it lay.
  assert not (kelso_env.main_repo / "ports-demo.klso.md").is_symlink()


def test_invalid_md_bundle_fails_stage_and_leaves_no_run_dir(kelso_env):
  bad = kelso_env.main_repo / "broken.klso.md"
  bad.write_text("just prose, no files\n")

  result = kelso_env.run("install", "broken")
  assert result.returncode == 1
  assert "does not contain any files" in result.stderr
  assert not (kelso_env.run_root / "broken").exists()


def test_inspect_md_bundle_by_path(kelso_env):
  source = write_md_bundle(kelso_env.root / "elsewhere")
  result = kelso_env.run("inspect", str(source))
  assert result.returncode == 0, result.stderr
  assert "alpine:latest" in result.stdout
