"""Single-file `.happ.md` bundles.

`load_happ` parses the markdown into files; staging extracts them so the run
tree is a plain directory and everything downstream of `stage` is unchanged.
"""

from pathlib import Path

import pytest

from harbor.lib.happ import HappFolder, HappMdFile, load_happ

MD_APP = """\
# A tiny happ, as one auditable file.

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Markdown demo"
description  = "A happ distributed as a single markdown file"

[volumes]
hello = { kind = "app", src = "bin/hello.sh" }

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "/app/hello.sh"]
volumes = { hello = "/app/hello.sh" }
restart = "no"
```

```bash happ_path="bin/hello.sh:+x"
echo "hello from markdown"
```
"""


def write_md_happ(parent: Path, app_id: str = "md-demo", body: str = MD_APP) -> Path:
  parent.mkdir(parents=True, exist_ok=True)
  path = parent / f"{app_id}.happ.md"
  path.write_text(body)
  return path


def test_load_happ_md_parses_files(tmp_path: Path):
  happ = load_happ(write_md_happ(tmp_path))
  assert isinstance(happ, HappMdFile)
  assert str(happ.app_id) == "md-demo"
  assert sorted(str(p) for p in happ.files()) == ["bin/hello.sh", "manifest.toml"]


def test_load_happ_folder_still_loads(tmp_path: Path):
  bundle = tmp_path / "plain.happ"
  bundle.mkdir()
  (bundle / "manifest.toml").write_text("[app]\nversion = '0.1.0'\n")
  happ = load_happ(bundle)
  assert isinstance(happ, HappFolder)
  assert str(happ.app_id) == "plain"


def test_md_extract_writes_files_and_exec_bit(tmp_path: Path):
  happ = load_happ(write_md_happ(tmp_path))
  target = tmp_path / "out"
  happ.extract_to(target)

  script = target / "bin" / "hello.sh"
  assert (target / "manifest.toml").is_file()
  assert script.is_file()
  assert script.stat().st_mode & 0o111
  assert not (target / "manifest.toml").stat().st_mode & 0o111
  assert "hello from markdown" in script.read_text()


def test_md_app_stack_builds_from_embedded_manifest(tmp_path: Path):
  stack = load_happ(write_md_happ(tmp_path)).app_stack()
  assert str(stack.app) == "md-demo"
  assert list(stack.run_units) == ["main"]
  assert stack.volumes["hello"].kind == "app"


@pytest.mark.parametrize(
  ("body", "problem"),
  [
    ("no code blocks here\n", "does not contain any files"),
    ('```sh happ_path="run.sh"\necho hi\n```\n', "missing a manifest.toml"),
    ('```toml happ_path="manifest.toml"\n[app]\n', "unclosed file block"),
    (
      '```toml happ_path="manifest.toml"\nx = 1\n```\n'
      '```sh happ_path="../escape.sh"\nboom\n```\n',
      "traverse up",
    ),
    (
      '```toml happ_path="manifest.toml"\nx = 1\n```\n'
      '```sh happ_path="/etc/passwd"\nboom\n```\n',
      "absolute file paths",
    ),
  ],
)
def test_load_happ_md_rejects_bad_documents(tmp_path: Path, body: str, problem: str):
  path = write_md_happ(tmp_path, body=body)
  with pytest.raises(ValueError, match=problem):
    load_happ(path)


def test_stage_md_happ_from_catalog(harbor_env):
  write_md_happ(harbor_env.root / "apps")

  result = harbor_env.run("stage", "md-demo")
  assert result.returncode == 0, result.stderr

  happ_dir = harbor_env.run_root / "md-demo" / "happ"
  assert (happ_dir / "manifest.toml").is_file()
  assert (happ_dir / "bin" / "hello.sh").stat().st_mode & 0o111


def test_stage_md_happ_by_path_links_catalog_entry(harbor_env):
  source = write_md_happ(harbor_env.root / "elsewhere")

  result = harbor_env.run("stage", str(source))
  assert result.returncode == 0, result.stderr

  entry = harbor_env.root / "apps" / "md-demo.happ.md"
  assert entry.is_symlink()
  assert entry.resolve() == source.resolve()
  assert (harbor_env.run_root / "md-demo" / "happ" / "manifest.toml").is_file()


def test_stage_conflicting_flavor_is_refused(harbor_env):
  """A `.happ.md` path whose id already has a catalog entry is refused."""
  write_md_happ(harbor_env.root / "apps", app_id="ports-demo")

  # ports-demo.happ (a fixture directory) already owns this id.
  result = harbor_env.run("stage", str(harbor_env.root / "apps" / "ports-demo.happ.md"))
  assert result.returncode == 1
  assert "already in the catalog" in result.stderr


def test_invalid_md_happ_fails_stage_and_leaves_no_run_dir(harbor_env):
  bad = harbor_env.root / "apps" / "broken.happ.md"
  bad.write_text("just prose, no files\n")

  result = harbor_env.run("stage", "broken")
  assert result.returncode == 1
  assert "does not contain any files" in result.stderr
  assert not (harbor_env.run_root / "broken").exists()


def test_inspect_md_happ_by_path(harbor_env):
  source = write_md_happ(harbor_env.root / "elsewhere")
  result = harbor_env.run("inspect", str(source))
  assert result.returncode == 0, result.stderr
  assert "alpine:latest" in result.stdout
