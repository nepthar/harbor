"""Tests for `harbor fetch`.

Every test runs against a local fake of the two GitHub endpoints harbor uses,
so the suite never touches the network. `harbor.lib.fetch.API_ROOT` and
`RAW_ROOT` are the only seams needed for that.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from harbor.cli import fetch as fetch_cli
from harbor.lib import fetch as fetch_lib
from harbor.lib.config import load_config_file
from harbor.lib.fetch import (
  HAPP_MD_CUTOFF_KB,
  MAX_FILE_BYTES,
  MAX_FILES,
  MAX_TOTAL_BYTES,
  GithubTarget,
  check_update,
  download_happ,
  ensure_destination_for,
  format_current,
  list_tree,
  parse_target,
  recorded_source,
  resolve_ref,
  source_is_pinned,
  split_pin,
)
from harbor.lib.harbor import HarborCtx

SHA = "a1b2c3d4" * 5  # 40 hex chars
NEW_SHA = "b" * 40
REPO_PATH = "examples/hello-world.happ"
TARGET = f"github:nepthar/harbor/main/{REPO_PATH}"

MANIFEST = b"""\
[app]
version      = "0.1.0"
display_name = "Hello world"
description  = "Says hello!"

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "echo hello"]
restart = "no"
"""

MANIFEST_WITH_APP_DIR = b"""\
[app]
version      = "0.1.0"
display_name = "Scripted"

[volumes]
app = { kind = "app", desc = "shipped alongside the manifest" }

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "/harbor/app/go.sh"]
volumes = { app = "/harbor/app" }
restart = "no"
"""


# --- fake github -----------------------------------------------------------


class FakeGithub:
  """The two endpoints harbor talks to, backed by an in-memory repo."""

  def __init__(self) -> None:
    self.sha = SHA
    self.blobs: dict[str, bytes] = {}  # keyed by path within the happ dir
    self.repo_files: dict[str, bytes] = {}  # whole files, keyed by repo path
    self.modes: dict[str, str] = {}
    self.extra_entries: list[dict] = []  # non-blob rows injected into a listing
    self.truncated = False
    self.tree_status = 200
    self.tree_error = "Not Found"
    self.commit_status = 200
    self.sizes: dict[str, int] = {}  # override a declared size (to lie)
    self.ratelimit_remaining = "59"
    self.requests: list[str] = []
    self._server: ThreadingHTTPServer | None = None

  # -- repo contents

  def add(self, path: str, content: bytes, mode: str = "100644") -> None:
    self.blobs[path] = content
    self.modes[path] = mode

  def hello_world(self) -> FakeGithub:
    self.add("manifest.toml", MANIFEST)
    return self

  # -- payloads

  def tree_payload(self) -> dict:
    tree = [
      {
        "path": path,
        "mode": self.modes[path],
        "type": "blob",
        "size": self.sizes.get(path, len(content)),
        "sha": "0" * 40,
      }
      for path, content in self.blobs.items()
    ]
    tree.extend(self.extra_entries)
    return {"sha": "t" * 40, "truncated": self.truncated, "tree": tree}

  # -- lifecycle

  @property
  def port(self) -> int:
    assert self._server is not None
    return self._server.server_address[1]

  def start(self) -> None:
    self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
    # socketserver's default 0.5s poll is what `shutdown()` waits on, so the
    # default costs half a second of teardown per test and nothing else.
    threading.Thread(
      target=self._server.serve_forever,
      kwargs={"poll_interval": 0.005},
      daemon=True,
    ).start()

  def stop(self) -> None:
    if self._server is not None:
      self._server.shutdown()
      self._server.server_close()

  @property
  def api_calls(self) -> list[str]:
    return [path for path in self.requests if path.startswith("/api/")]


def _handler_for(fake: FakeGithub):
  class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep pytest output clean
      pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
      self.send_response(status)
      self.send_header("Content-Type", ctype)
      self.send_header("Content-Length", str(len(body)))
      self.send_header("x-ratelimit-remaining", fake.ratelimit_remaining)
      self.end_headers()
      self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
      path = unquote(urlparse(self.path).path)
      fake.requests.append(path)

      if path.startswith("/api/"):
        return self._api(path[len("/api/") :].split("/"))
      if path.startswith("/raw/"):
        return self._raw(path[len("/raw/") :].split("/"))
      self._send(404, b"{}", "application/json")

    def _api(self, parts: list[str]) -> None:
      # repos/<user>/<repo>/commits/<ref>
      if len(parts) >= 5 and parts[3] == "commits":
        if fake.commit_status != 200:
          return self._error(fake.commit_status)
        return self._send(200, fake.sha.encode(), "text/plain")

      # repos/<user>/<repo>/git/trees/<sha>:<path>
      if len(parts) >= 6 and parts[3] == "git" and parts[4] == "trees":
        if fake.tree_status != 200:
          return self._error(fake.tree_status, fake.tree_error)
        body = json.dumps(fake.tree_payload()).encode()
        return self._send(200, body, "application/json")

      self._error(404)

    def _raw(self, parts: list[str]) -> None:
      # <user>/<repo>/<sha>/<repo path...>/<entry path...>
      rest = "/".join(parts[3:])
      whole = fake.repo_files.get(rest)
      if whole is not None:
        return self._send(200, whole, "application/octet-stream")
      prefix = f"{REPO_PATH}/"
      if not rest.startswith(prefix):
        return self._error(404)
      content = fake.blobs.get(rest[len(prefix) :])
      if content is None:
        return self._error(404)
      self._send(200, content, "application/octet-stream")

    def _error(self, status: int, message: str = "Not Found") -> None:
      body = json.dumps({"message": message}).encode()
      self._send(status, body, "application/json")

  return Handler


@pytest.fixture
def github(monkeypatch):
  fake = FakeGithub()
  fake.start()
  monkeypatch.setattr(fetch_lib, "API_ROOT", f"http://127.0.0.1:{fake.port}/api")
  monkeypatch.setattr(fetch_lib, "RAW_ROOT", f"http://127.0.0.1:{fake.port}/raw")
  yield fake
  fake.stop()


@pytest.fixture
def ctx(harbor_env) -> HarborCtx:
  return HarborCtx(load_config_file(harbor_env.config))


class FakeConn:
  """A Conn that records output and answers the install prompt."""

  def __init__(self, answer: str = "y") -> None:
    self.answer = answer
    self.lines: list[str] = []
    self.prompted = False

  def out(self, data: str) -> None:
    self.lines.append(data)

  def err(self, data: str) -> None:
    self.lines.append(data)

  def read(self, prompt: str = "") -> str:
    self.prompted = True
    return self.answer

  @property
  def text(self) -> str:
    return "\n".join(self.lines)


def fetch(ctx, conn, target: str = TARGET, *, yes: bool = True) -> None:
  args = argparse.Namespace(target=target, yes=yes)
  fetch_cli.run(args, ctx, conn)


def a_target(path: str = REPO_PATH, ref: str = "main") -> GithubTarget:
  return parse_target(f"github:nepthar/harbor/{ref}/{path}")


# --- addressing ------------------------------------------------------------


def test_parse_target_reads_repo_coordinates():
  target = parse_target(TARGET)
  assert (target.user, target.repo, target.ref) == ("nepthar", "harbor", "main")
  assert target.path == ("examples", "hello-world.happ")
  assert target.app_id == "hello-world"


def test_app_id_keeps_dots_in_a_reverse_fqdn_name():
  target = a_target("dist/pw.zyx.hello-world.happ")
  assert target.app_id == "pw.zyx.hello-world"


@pytest.mark.parametrize(
  "raw",
  [
    "https://happs.example.com/hello-world.happ",
    "http://10.0.0.5:8080/hello-world.happ",
    "hello-world.happ",
    "./examples/hello-world.happ",
  ],
)
def test_only_github_targets_are_accepted(raw):
  with pytest.raises(ValueError, match="Unsupported fetch target"):
    parse_target(raw)


@pytest.mark.parametrize(
  "raw",
  [
    "github:nepthar",
    "github:nepthar/harbor",
    "github:nepthar/harbor/main",
  ],
)
def test_a_target_must_name_user_repo_ref_and_path(raw):
  with pytest.raises(ValueError, match="Malformed GitHub target"):
    parse_target(raw)


def test_the_last_segment_must_be_a_happ_directory():
  with pytest.raises(ValueError, match="does not name a happ"):
    parse_target("github:nepthar/harbor/main/examples/hello-world")


@pytest.mark.parametrize("suffix", [".happ", ".happ.md"])
def test_a_bare_suffix_is_not_a_name(suffix):
  with pytest.raises(ValueError, match="does not name a happ"):
    parse_target(f"github:nepthar/harbor/main/examples/{suffix}")


@pytest.mark.parametrize("segment", ["..", ".", ""])
def test_path_traversal_is_refused_in_a_target(segment):
  with pytest.raises(ValueError, match="Malformed path segment"):
    parse_target(f"github:nepthar/harbor/main/{segment}/hello-world.happ")


@pytest.mark.parametrize(
  "raw",
  ["github:-bad/harbor/main/x.happ", "github:nepthar/re po/main/x.happ"],
)
def test_github_names_are_validated(raw):
  with pytest.raises(ValueError, match="Invalid GitHub"):
    parse_target(raw)


def test_a_repo_name_may_start_and_end_with_underscores():
  target = parse_target("github:nepthar/_vibes_/main/io.nthr.jrnl.happ")
  assert target.repo == "_vibes_"
  assert target.app_id == "io.nthr.jrnl"


# --- ref resolution --------------------------------------------------------


def test_a_branch_is_resolved_to_a_commit_sha(github):
  assert resolve_ref(a_target()) == SHA
  assert len(github.api_calls) == 1


def test_a_pinned_sha_costs_no_api_call(github):
  target = a_target(ref=SHA)
  assert resolve_ref(target) == SHA
  assert github.api_calls == []


def test_split_pin_strips_a_trailing_sha():
  spec, pin = split_pin(f"{TARGET}@{SHA}")
  assert spec == TARGET
  assert pin == SHA
  assert split_pin(TARGET) == (TARGET, None)
  assert split_pin("hello-world") == ("hello-world", None)
  assert split_pin(f"hello-world@{SHA}") == ("hello-world", SHA)


def test_split_pin_leaves_a_non_sha_suffix_in_place():
  raw = "hello-world@v1.2.3"
  assert split_pin(raw) == (raw, None)


def test_recorded_source_pins_a_sha_ref():
  spec = f"github:nepthar/harbor/{SHA}/{REPO_PATH}"
  assert recorded_source(spec, None) == f"{spec}@{SHA}"
  assert recorded_source(TARGET, None) == TARGET
  assert recorded_source(TARGET, SHA) == f"{TARGET}@{SHA}"
  assert source_is_pinned(f"{TARGET}@{SHA}")
  assert not source_is_pinned(TARGET)


# --- listing safety --------------------------------------------------------


def test_listing_returns_blobs_and_skips_directories(github):
  github.hello_world()
  github.add("app/go.sh", b"#!/bin/sh\n", mode="100755")
  github.extra_entries.append(
    {"path": "app", "mode": "040000", "type": "tree", "sha": "0" * 40}
  )

  entries = list_tree(a_target(), SHA)

  assert sorted(e.path for e in entries) == ["app/go.sh", "manifest.toml"]
  assert [e.executable for e in entries if e.path == "app/go.sh"] == [True]


@pytest.mark.parametrize(
  ("mode", "kind"),
  [("120000", "symlink"), ("160000", "submodule")],
)
def test_symlinks_and_submodules_are_refused(github, mode, kind):
  github.hello_world()
  github.add("sneaky", b"/etc/passwd", mode=mode)

  with pytest.raises(ValueError, match="not a regular file or directory"):
    list_tree(a_target(), SHA)


def test_a_truncated_listing_is_refused(github):
  github.hello_world()
  github.truncated = True

  with pytest.raises(ValueError, match="too large to list"):
    list_tree(a_target(), SHA)


@pytest.mark.parametrize("path", ["../escape", "nested/../../escape", "/etc/passwd"])
def test_paths_escaping_the_happ_are_refused(github, path):
  github.hello_world()
  github.add(path, b"x")

  with pytest.raises(ValueError, match="unsafe path|Malformed path segment"):
    list_tree(a_target(), SHA)


def test_an_oversized_file_is_refused_before_download(github):
  github.hello_world()
  github.add("big.bin", b"x")
  github.sizes["big.bin"] = MAX_FILE_BYTES + 1

  with pytest.raises(ValueError, match="per-file limit"):
    list_tree(a_target(), SHA)
  assert not any(path.startswith("/raw/") for path in github.requests)


def test_an_oversized_happ_is_refused(github):
  github.hello_world()
  for i in range(9):
    github.add(f"pad{i}.bin", b"x")
    github.sizes[f"pad{i}.bin"] = MAX_TOTAL_BYTES // 8

  with pytest.raises(ValueError, match="over the .* limit for a happ"):
    list_tree(a_target(), SHA)


def test_too_many_files_are_refused(github):
  github.hello_world()
  for i in range(MAX_FILES):
    github.add(f"f{i}", b"x")

  with pytest.raises(ValueError, match="over the .*-file limit"):
    list_tree(a_target(), SHA)


def test_a_directory_without_a_manifest_is_not_a_happ(github):
  github.add("README.md", b"# nope\n")

  with pytest.raises(ValueError, match="no manifest.toml"):
    list_tree(a_target(), SHA)


def test_a_missing_path_reports_not_found(github):
  github.tree_status = 404

  with pytest.raises(ValueError, match="Not found"):
    list_tree(a_target(), SHA)


def test_a_path_naming_a_file_surfaces_githubs_reason(github):
  github.tree_status = 422
  github.tree_error = "Invalid object requested. SHA must identify a commit or a tree."

  with pytest.raises(ValueError, match="SHA must identify a commit or a tree"):
    list_tree(a_target(), SHA)


def test_an_exhausted_rate_limit_is_named_as_such(github):
  github.tree_status = 403
  github.ratelimit_remaining = "0"

  with pytest.raises(ValueError, match="rate limit reached"):
    list_tree(a_target(), SHA)


def test_a_forbidden_request_is_not_reported_as_a_rate_limit(github):
  """A 403 with budget left is something else -- say so, don't misdirect."""
  github.tree_status = 403
  github.tree_error = "Repository access blocked"
  github.ratelimit_remaining = "59"

  with pytest.raises(ValueError, match="Repository access blocked"):
    list_tree(a_target(), SHA)


# --- downloading -----------------------------------------------------------


def test_download_writes_the_happ_and_keeps_the_executable_bit(github, harbor_env):
  github.hello_world()
  github.add("app/go.sh", b"#!/bin/sh\necho hi\n", mode="100755")

  fetched = download_happ(a_target(), harbor_env.root / "apps")

  assert fetched.path.name == "hello-world.happ"
  assert (fetched.path / "manifest.toml").read_bytes() == MANIFEST
  assert (fetched.path / "app" / "go.sh").stat().st_mode & 0o111
  assert not (fetched.path / "manifest.toml").stat().st_mode & 0o111
  assert fetched.sha == SHA
  assert fetched.files == 2


def test_scratch_dir_is_cleaned_up_when_a_download_fails(github, harbor_env):
  github.hello_world()
  # Listed by the tree but absent from the blob store: a 404 mid-download.
  github.extra_entries.append(
    {"path": "gone.txt", "mode": "100644", "type": "blob", "size": 1, "sha": "0" * 40}
  )
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="Not found"):
    download_happ(a_target(), apps_root)

  assert list(apps_root.glob(".fetch-*")) == []


def test_a_blob_larger_than_its_listing_is_cut_off(github, harbor_env):
  """The declared size gates the download; the stream is capped regardless."""
  github.hello_world()
  github.add("big.bin", b"x" * (MAX_FILE_BYTES + 1))
  github.sizes["big.bin"] = 1  # the listing lies
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="more than the .* limit"):
    download_happ(a_target(), apps_root)

  assert list(apps_root.glob(".fetch-*")) == []


def test_an_installed_app_is_never_overwritten(harbor_env):
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="already exists"):
    ensure_destination_for("ports-demo", apps_root)


def test_a_symlink_catalog_entry_is_not_overwritten(harbor_env):
  """`stage <path>` leaves a link in apps/; fetch must not clobber it.

  A broken link is the sharp case: `exists()` follows it and returns False,
  so a naive occupancy check would fall through to `os.replace` and ENOTDIR.
  """
  apps_root = harbor_env.root / "apps"
  dest = apps_root / "hello-world.happ"
  dest.symlink_to("/no/such/working-tree")

  with pytest.raises(ValueError, match="already exists at .*hello-world.happ"):
    ensure_destination_for("hello-world", apps_root)

  assert dest.is_symlink()


# --- the command ------------------------------------------------------------


def test_fetch_installs_a_happ(github, ctx, harbor_env):
  github.hello_world()
  conn = FakeConn()

  fetch(ctx, conn)

  installed = harbor_env.root / "apps" / "hello-world.happ"
  assert (installed / "manifest.toml").read_bytes() == MANIFEST
  assert "alpine:latest" in conn.text
  assert f"nepthar/harbor@{SHA[:8]}" in conn.text
  assert "Installed hello-world" in conn.text
  record = ctx.harbor_db.get_app_source("hello-world")
  assert record == {
    "source": TARGET,
    "current": format_current("0.1.0", SHA),
  }


def test_fetch_installs_files_shipped_beside_the_manifest(github, ctx, harbor_env):
  github.add("manifest.toml", MANIFEST_WITH_APP_DIR)
  github.add("app/go.sh", b"#!/bin/sh\necho hi\n", mode="100755")
  conn = FakeConn()

  fetch(ctx, conn)

  installed = harbor_env.root / "apps" / "hello-world.happ"
  assert (installed / "app" / "go.sh").stat().st_mode & 0o111


def test_fetch_asks_before_installing(github, ctx, harbor_env):
  github.hello_world()
  conn = FakeConn(answer="y")

  fetch(ctx, conn, yes=False)

  assert conn.prompted
  assert (harbor_env.root / "apps" / "hello-world.happ").is_dir()


def test_declining_installs_nothing_and_leaves_no_scratch_dir(github, ctx, harbor_env):
  github.hello_world()
  conn = FakeConn(answer="n")

  fetch(ctx, conn, yes=False)

  apps_root = harbor_env.root / "apps"
  assert not (apps_root / "hello-world.happ").exists()
  assert list(apps_root.glob(".fetch-*")) == []
  assert "Not installed." in conn.text


def test_yes_skips_the_prompt(github, ctx, harbor_env):
  github.hello_world()
  conn = FakeConn()

  fetch(ctx, conn, yes=True)

  assert not conn.prompted


def test_a_manifest_claiming_another_id_is_refused(github, ctx, harbor_env):
  github.add("manifest.toml", MANIFEST.replace(b"[app]", b'[app]\napp_id = "other"'))
  conn = FakeConn()

  with pytest.raises(ValueError, match="does not match app_id"):
    fetch(ctx, conn)

  apps_root = harbor_env.root / "apps"
  assert not (apps_root / "hello-world.happ").exists()
  assert list(apps_root.glob(".fetch-*")) == []


def test_an_invalid_manifest_leaves_nothing_behind(github, ctx, harbor_env):
  github.add("manifest.toml", b"this is not toml {{{")
  conn = FakeConn()

  with pytest.raises(ValueError, match="not valid TOML"):
    fetch(ctx, conn)

  apps_root = harbor_env.root / "apps"
  assert list(apps_root.glob(".fetch-*")) == []


def test_a_collision_fails_before_spending_rate_limit(github, ctx):
  conn = FakeConn()
  target = f"github:nepthar/harbor/main/{Path(REPO_PATH).parent}/ports-demo.happ"

  with pytest.raises(ValueError, match="already exists"):
    fetch(ctx, conn, target)

  assert github.requests == []


def test_a_fetched_happ_is_an_ordinary_happ(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())

  catalog = harbor_env.run("catalog")
  assert catalog.returncode == 0, catalog.stderr
  assert "hello-world" in catalog.stdout

  started = harbor_env.run("start", "hello-world")
  assert started.returncode == 0, started.stderr
  assert (harbor_env.run_root / "hello-world" / "compose.yml").is_file()


# --- single-file .happ.md targets -------------------------------------------

MD_REPO_PATH = "examples/hello-md.happ.md"
MD_TARGET = f"github:nepthar/harbor/main/{MD_REPO_PATH}"

MD_HAPP = b"""\
# Hello from markdown

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Hello md"
description  = "A single-file happ"

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "echo hello"]
restart = "no"
```
"""


def test_parse_target_reads_md_coordinates():
  target = parse_target(MD_TARGET)
  assert target.path == ("examples", "hello-md.happ.md")
  assert target.is_single_file
  assert target.suffix == ".happ.md"
  assert target.app_id == "hello-md"


def test_md_fetch_skips_the_tree_listing(github, harbor_env):
  github.repo_files[MD_REPO_PATH] = MD_HAPP

  fetched = download_happ(a_target(MD_REPO_PATH), harbor_env.root / "apps")

  assert fetched.path.name == "hello-md.happ.md"
  assert fetched.path.read_bytes() == MD_HAPP
  assert fetched.files == 1
  # One call resolves the ref; the single blob comes off the raw host.
  assert len(github.api_calls) == 1
  assert not any("/git/trees/" in path for path in github.api_calls)


def test_fetch_installs_an_md_happ(github, ctx, harbor_env):
  github.repo_files[MD_REPO_PATH] = MD_HAPP
  conn = FakeConn()

  fetch(ctx, conn, MD_TARGET)

  installed = harbor_env.root / "apps" / "hello-md.happ.md"
  assert installed.read_bytes() == MD_HAPP
  assert "alpine:latest" in conn.text
  assert "Installed hello-md" in conn.text


def test_a_fetched_md_happ_is_an_ordinary_happ(github, ctx, harbor_env):
  github.repo_files[MD_REPO_PATH] = MD_HAPP
  fetch(ctx, FakeConn(), MD_TARGET)

  started = harbor_env.run("start", "hello-md")
  assert started.returncode == 0, started.stderr
  assert (harbor_env.run_root / "hello-md" / "compose.yml").is_file()


def test_an_oversized_md_happ_is_cut_off(github, harbor_env):
  github.repo_files[MD_REPO_PATH] = b"x" * (HAPP_MD_CUTOFF_KB * 1024 + 1)
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="more than the .* limit"):
    download_happ(a_target(MD_REPO_PATH), apps_root)

  assert list(apps_root.glob(".fetch-*")) == []


def test_an_invalid_md_happ_leaves_nothing_behind(github, ctx, harbor_env):
  github.repo_files[MD_REPO_PATH] = b"just prose, no file blocks\n"
  conn = FakeConn()

  with pytest.raises(ValueError, match="does not contain any files"):
    fetch(ctx, conn, MD_TARGET)

  apps_root = harbor_env.root / "apps"
  assert not (apps_root / "hello-md.happ.md").exists()
  assert list(apps_root.glob(".fetch-*")) == []


def test_an_md_target_colliding_with_a_folder_happ_is_refused(github, ctx):
  """One id, one catalog entry -- whatever flavor already owns it."""
  conn = FakeConn()
  target = "github:nepthar/harbor/main/examples/ports-demo.happ.md"

  with pytest.raises(ValueError, match="already exists"):
    fetch(ctx, conn, target)

  assert github.requests == []


def test_a_folder_target_colliding_with_an_md_happ_is_refused(harbor_env):
  apps_root = harbor_env.root / "apps"
  (apps_root / "solo.happ.md").write_bytes(MD_HAPP)

  with pytest.raises(ValueError, match="already exists"):
    ensure_destination_for("solo", apps_root)


# --- cli wiring -------------------------------------------------------------


def test_the_cli_rejects_a_non_github_target(harbor_env):
  result = harbor_env.run("fetch", "https://happs.example.com/x.happ")

  assert result.returncode == 1
  assert "Don't know how to fetch" in result.stderr


def test_the_cli_rejects_a_pathlike_target(harbor_env):
  result = harbor_env.run("fetch", "hello-world.happ")

  assert result.returncode == 1
  assert "Don't know how to fetch" in result.stderr


def test_the_cli_rejects_a_non_sha_pin(harbor_env):
  result = harbor_env.run("fetch", "hello-world@v1")

  assert result.returncode == 1
  assert "40-character commit sha" in result.stderr


# --- update ----------------------------------------------------------------


def test_fetch_app_id_is_a_no_op_when_the_commit_is_unchanged(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())
  before = len(github.requests)
  conn = FakeConn()

  fetch(ctx, conn, "hello-world")

  assert not conn.prompted
  assert f"already at {format_current('0.1.0', SHA)}" in conn.text
  assert len(github.requests) == before + 1  # resolve the branch, nothing else
  assert not any("/git/trees/" in path for path in github.requests[before:])


def test_fetch_app_id_replaces_the_catalog_when_main_moves(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())
  github.sha = NEW_SHA
  github.add("manifest.toml", MANIFEST.replace(b"0.1.0", b"0.2.0"))
  conn = FakeConn()

  fetch(ctx, conn, "hello-world")

  installed = harbor_env.root / "apps" / "hello-world.happ"
  assert b'version      = "0.2.0"' in (installed / "manifest.toml").read_bytes()
  assert not conn.prompted
  assert "Updated hello-world" in conn.text
  assert f" - {format_current('0.1.0', SHA)}" in conn.text
  assert f" + {format_current('0.2.0', NEW_SHA)}" in conn.text
  assert "snapshot" not in conn.text.lower()
  record = ctx.harbor_db.get_app_source("hello-world")
  assert record == {
    "source": TARGET,
    "current": format_current("0.2.0", NEW_SHA),
  }


def test_fetch_app_id_does_not_touch_the_run_dir(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())
  started = harbor_env.run("start", "hello-world")
  assert started.returncode == 0, started.stderr
  staged = (harbor_env.run_root / "hello-world" / "happ" / "manifest.toml").read_bytes()

  github.sha = NEW_SHA
  github.add("manifest.toml", MANIFEST.replace(b"0.1.0", b"0.2.0"))
  conn = FakeConn()
  fetch(ctx, conn, "hello-world")

  assert (
    harbor_env.run_root / "hello-world" / "happ" / "manifest.toml"
  ).read_bytes() == staged
  assert "harbor snapshot hello-world" in conn.text
  assert "harbor stage hello-world" in conn.text
  assert "harbor start hello-world" in conn.text


def test_a_pinned_fetch_does_not_follow_main(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn(), f"{TARGET}@{SHA}")
  record = ctx.harbor_db.get_app_source("hello-world")
  assert record["source"] == f"{TARGET}@{SHA}"

  github.sha = NEW_SHA
  before = len(github.requests)
  conn = FakeConn()
  fetch(ctx, conn, "hello-world")

  assert f"pinned at {format_current('0.1.0', SHA)}" in conn.text
  assert len(github.requests) == before
  assert ctx.harbor_db.get_app_source("hello-world")["current"] == format_current(
    "0.1.0", SHA
  )


def test_fetch_app_id_at_sha_pins_without_redownloading(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())
  before = len(github.requests)
  conn = FakeConn()

  fetch(ctx, conn, f"hello-world@{SHA}")

  assert f"Pinned hello-world at {format_current('0.1.0', SHA)}" in conn.text
  assert not any("/git/trees/" in path for path in github.requests[before:])
  assert ctx.harbor_db.get_app_source("hello-world")["source"] == f"{TARGET}@{SHA}"


def test_fetch_app_id_at_sha_moves_a_pin(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn(), f"{TARGET}@{SHA}")
  github.add("manifest.toml", MANIFEST.replace(b"0.1.0", b"0.2.0"))
  conn = FakeConn()

  fetch(ctx, conn, f"hello-world@{NEW_SHA}")

  assert "Updated hello-world" in conn.text
  record = ctx.harbor_db.get_app_source("hello-world")
  assert record == {
    "source": f"{TARGET}@{NEW_SHA}",
    "current": format_current("0.2.0", NEW_SHA),
  }


def test_fetch_app_id_without_a_source_is_refused(ctx, harbor_env):
  conn = FakeConn()
  with pytest.raises(ValueError, match="no recorded GitHub source"):
    fetch(ctx, conn, "ports-demo")
  assert not conn.prompted


def test_fetch_unknown_app_id_names_github(harbor_env):
  result = harbor_env.run("fetch", "no-such-app")

  assert result.returncode == 1
  assert "No app found" in result.stderr
  assert "github:" in result.stderr


def test_rm_leaves_fetch_source_so_the_catalog_can_still_update(
  github, ctx, harbor_env
):
  github.hello_world()
  fetch(ctx, FakeConn())
  assert harbor_env.run("start", "hello-world").returncode == 0
  removed = harbor_env.run("rm", "hello-world", "-y")
  assert removed.returncode == 0, removed.stderr

  github.sha = NEW_SHA
  github.add("manifest.toml", MANIFEST.replace(b"0.1.0", b"0.2.0"))
  conn = FakeConn()
  fetch(ctx, conn, "hello-world")

  assert "Updated hello-world" in conn.text
  assert "snapshot" not in conn.text.lower()
  assert (
    b"0.2.0"
    in (harbor_env.root / "apps" / "hello-world.happ" / "manifest.toml").read_bytes()
  )


# --- check, without applying ------------------------------------------------


def test_check_update_is_quiet_when_the_commit_is_unchanged(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())
  before = len(github.requests)

  check = check_update("hello-world", ctx)

  assert check.available is False
  assert check.pinned is False
  assert check.remote is None
  assert f"already at {format_current('0.1.0', SHA)}" in check.message
  assert len(github.requests) == before + 1
  assert not any("/git/trees/" in path for path in github.requests[before:])


def test_check_update_reports_a_moved_ref_without_installing(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())
  github.sha = NEW_SHA
  github.add("manifest.toml", MANIFEST.replace(b"0.1.0", b"0.2.0"))

  check = check_update("hello-world", ctx)

  assert check.available is True
  assert check.current == format_current("0.1.0", SHA)
  assert check.remote == format_current("0.2.0", NEW_SHA)
  assert 'version      = "0.1.0"' in check.current_manifest
  assert 'version      = "0.2.0"' in (check.remote_manifest or "")
  installed = harbor_env.root / "apps" / "hello-world.happ" / "manifest.toml"
  assert installed.read_bytes() == MANIFEST
  assert list((harbor_env.root / "apps").glob(".fetch-*")) == []


def test_check_update_respects_a_pin(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn(), f"{TARGET}@{SHA}")
  github.sha = NEW_SHA
  before = len(github.requests)

  check = check_update("hello-world", ctx)

  assert check.available is False
  assert check.pinned is True
  assert f"pinned at {format_current('0.1.0', SHA)}" in check.message
  assert len(github.requests) == before


def test_check_update_refuses_an_app_without_a_source(ctx):
  with pytest.raises(ValueError, match="no recorded GitHub source"):
    check_update("ports-demo", ctx)


# --- over the admin API ----------------------------------------------------


@pytest.fixture
def api_client(harbor_env):
  """The admin API over this harbor root, with a runner the test drains."""
  from fastapi.testclient import TestClient

  from harbor.daemon.api import create_app
  from harbor.jobs import JobRunner

  def factory() -> HarborCtx:
    return HarborCtx(load_config_file(harbor_env.config))

  runner = JobRunner(factory)
  return TestClient(create_app(factory, runner)), runner


def test_preview_reads_a_target_without_installing_it(github, api_client, harbor_env):
  github.hello_world()
  client, _ = api_client

  body = client.post("/catalog/preview", json={"target": TARGET}).json()

  assert body["app_id"] == "hello-world"
  assert body["display_name"] == "Hello world"
  assert body["version"] == "0.1.0"
  assert body["source"] == "github:nepthar"
  assert body["installed"] is False
  assert body["conflict"] is None
  assert body["sha"] == SHA
  assert 'image   = "alpine:latest"' in body["manifest"]

  # Looking is not installing, and the scratch dir does not outlive the request.
  apps = harbor_env.root / "apps"
  assert not (apps / "hello-world.happ").exists()
  assert not list(apps.glob(".fetch-*"))


def test_preview_reports_a_conflict_instead_of_refusing(github, api_client, ctx):
  """The card still renders; it just cannot offer an install button."""
  github.hello_world()
  client, runner = api_client
  client.post("/jobs", json={"verb": "fetch", "args": {"target": TARGET, "yes": "1"}})
  runner.run_pending()

  body = client.post("/catalog/preview", json={"target": TARGET}).json()

  assert body["app_id"] == "hello-world"
  assert "already in the apps app source" in body["conflict"]
  assert 'image   = "alpine:latest"' in body["manifest"]


def test_preview_refuses_a_target_that_is_not_a_github_url(api_client):
  client, _ = api_client
  response = client.post("/catalog/preview", json={"target": "hello-world"})
  assert response.status_code == 400
  assert "expected a github: target" in response.json()["error"]


def test_fetch_job_needs_an_explicit_yes(github, api_client, harbor_env):
  """No prompt exists over HTTP, so consent has to be in the submission."""
  github.hello_world()
  client, _ = api_client

  response = client.post("/jobs", json={"verb": "fetch", "args": {"target": TARGET}})
  assert response.status_code == 400
  assert "resubmit with yes=1" in response.json()["error"]
  assert not (harbor_env.root / "apps" / "hello-world.happ").exists()


def test_fetch_job_installs_and_records_its_source(github, api_client, ctx, harbor_env):
  github.hello_world()
  client, runner = api_client

  response = client.post(
    "/jobs", json={"verb": "fetch", "args": {"target": TARGET, "yes": "1"}}
  )
  runner.run_pending()
  job = client.get(f"/jobs/{response.json()['id']}").json()

  assert job["state"] == "done", job["error"]
  log_text = (ctx.config.activity_root / job["log"]).read_text()
  assert "Installed hello-world" in log_text
  installed = harbor_env.root / "apps" / "hello-world.happ"
  assert (installed / "manifest.toml").read_bytes() == MANIFEST
  assert ctx.harbor_db.get_app_source("hello-world") == {
    "source": TARGET,
    "current": format_current("0.1.0", SHA),
  }

  # Fetched, not staged: nothing runs until someone asks for it separately.
  assert not (harbor_env.root / "run" / "hello-world").exists()


def test_fetch_job_refuses_a_path_target(api_client):
  client, _ = api_client
  response = client.post(
    "/jobs", json={"verb": "fetch", "args": {"target": "./local.happ", "yes": "1"}}
  )
  assert response.status_code == 400
  assert "expected a github: target" in response.json()["error"]


def test_check_reads_a_moved_ref_without_installing(github, api_client, harbor_env):
  github.hello_world()
  client, runner = api_client
  client.post("/jobs", json={"verb": "fetch", "args": {"target": TARGET, "yes": "1"}})
  runner.run_pending()
  github.sha = NEW_SHA
  github.add("manifest.toml", MANIFEST.replace(b"0.1.0", b"0.2.0"))

  response = client.post("/catalog/check", json={"app": "hello-world"})
  assert response.status_code == 200, response.text
  body = response.json()

  assert body["available"] is True
  assert body["pinned"] is False
  assert body["current_version"] == "0.1.0"
  assert body["current_sha"] == SHA
  assert body["remote_version"] == "0.2.0"
  assert body["remote_sha"] == NEW_SHA
  assert '-version      = "0.1.0"' in body["diff"]
  assert '+version      = "0.2.0"' in body["diff"]
  assert 'version      = "0.2.0"' in body["remote_manifest"]
  installed = harbor_env.root / "apps" / "hello-world.happ" / "manifest.toml"
  assert installed.read_bytes() == MANIFEST
  assert list((harbor_env.root / "apps").glob(".fetch-*")) == []


def test_check_is_quiet_when_already_current(github, api_client):
  github.hello_world()
  client, runner = api_client
  client.post("/jobs", json={"verb": "fetch", "args": {"target": TARGET, "yes": "1"}})
  runner.run_pending()

  body = client.post("/catalog/check", json={"app": "hello-world"}).json()

  assert body["available"] is False
  assert body["diff"] is None
  assert "already at" in body["message"]


def test_check_refuses_a_local_app(api_client):
  client, _ = api_client
  response = client.post("/catalog/check", json={"app": "ports-demo"})
  assert response.status_code == 400
  assert "no recorded GitHub source" in response.json()["error"]
