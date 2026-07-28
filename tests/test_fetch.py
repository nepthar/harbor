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
  MAX_FILE_BYTES,
  MAX_FILES,
  MAX_TOTAL_BYTES,
  GithubTarget,
  destination_for,
  list_tree,
  parse_target,
  resolve_ref,
  stage_happ,
)
from harbor.lib.harbor import HarborCtx

SHA = "a1b2c3d4" * 5  # 40 hex chars
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
    threading.Thread(target=self._server.serve_forever, daemon=True).start()

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
  return HarborCtx(load_config_file(harbor_env.config, "test"))


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
  with pytest.raises(ValueError, match="does not name a happ directory"):
    parse_target("github:nepthar/harbor/main/examples/hello-world")


def test_a_bare_suffix_is_not_a_name():
  with pytest.raises(ValueError, match="does not name a happ directory"):
    parse_target("github:nepthar/harbor/main/examples/.happ")


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


# --- ref resolution --------------------------------------------------------


def test_a_branch_is_resolved_to_a_commit_sha(github):
  assert resolve_ref(a_target()) == SHA
  assert len(github.api_calls) == 1


def test_a_pinned_sha_costs_no_api_call(github):
  target = a_target(ref=SHA)
  assert resolve_ref(target) == SHA
  assert github.api_calls == []


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


# --- staging ---------------------------------------------------------------


def test_staging_writes_the_happ_and_keeps_the_executable_bit(github, harbor_env):
  github.hello_world()
  github.add("app/go.sh", b"#!/bin/sh\necho hi\n", mode="100755")

  staged = stage_happ(a_target(), harbor_env.root / "apps")

  assert staged.path.name == "hello-world.happ"
  assert (staged.path / "manifest.toml").read_bytes() == MANIFEST
  assert (staged.path / "app" / "go.sh").stat().st_mode & 0o111
  assert not (staged.path / "manifest.toml").stat().st_mode & 0o111
  assert staged.sha == SHA
  assert staged.files == 2


def test_staging_is_cleaned_up_when_a_download_fails(github, harbor_env):
  github.hello_world()
  # Listed by the tree but absent from the blob store: a 404 mid-download.
  github.extra_entries.append(
    {"path": "gone.txt", "mode": "100644", "type": "blob", "size": 1, "sha": "0" * 40}
  )
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="Not found"):
    stage_happ(a_target(), apps_root)

  assert list(apps_root.glob(".fetch-*")) == []


def test_a_blob_larger_than_its_listing_is_cut_off(github, harbor_env):
  """The declared size gates the download; the stream is capped regardless."""
  github.hello_world()
  github.add("big.bin", b"x" * (MAX_FILE_BYTES + 1))
  github.sizes["big.bin"] = 1  # the listing lies
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="more than the .* its listing declared"):
    stage_happ(a_target(), apps_root)

  assert list(apps_root.glob(".fetch-*")) == []


def test_an_installed_app_is_never_overwritten(harbor_env):
  apps_root = harbor_env.root / "apps"

  with pytest.raises(ValueError, match="already installed"):
    destination_for("ports-demo", apps_root)


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


def test_declining_installs_nothing_and_leaves_no_staging(github, ctx, harbor_env):
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

  with pytest.raises(ValueError, match="already installed"):
    fetch(ctx, conn, target)

  assert github.requests == []


def test_a_fetched_happ_is_an_ordinary_happ(github, ctx, harbor_env):
  github.hello_world()
  fetch(ctx, FakeConn())

  catalog = harbor_env.run("catalog")
  assert catalog.returncode == 0, catalog.stderr
  assert "hello-world" in catalog.stdout

  up = harbor_env.run("up", "hello-world")
  assert up.returncode == 0, up.stderr
  assert (harbor_env.run_root / "hello-world" / "compose.yml").is_file()


# --- cli wiring -------------------------------------------------------------


def test_the_cli_rejects_a_non_github_target(harbor_env):
  result = harbor_env.run("fetch", "https://happs.example.com/x.happ")

  assert result.returncode == 1
  assert "Unsupported fetch target" in result.stderr
  assert "github:<user>/<repo>/<ref>" in result.stderr
