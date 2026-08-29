"""Tests for application repos: addressing, mirroring, and what a mirror holds.

Every test runs against a local fake of the two GitHub endpoints harbor uses,
so the suite never touches the network. `harbor.lib.github.API_ROOT` and
`RAW_ROOT` are the only seams needed for that.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import pytest

from harbor.lib import github as github_lib
from harbor.lib.config import load_config_file
from harbor.lib.github import MAX_FILE_BYTES, list_tree, resolve_ref
from harbor.lib.harbor import HarborCtx
from harbor.lib.repo import (
  MAX_HAPPS,
  MAX_REPO_BYTES,
  MAX_REPO_FILES,
  Repo,
  group_happs,
  mirror,
  name_from_url,
  parse_github_url,
)

SHA = "a1b2c3d4" * 5  # 40 hex chars
NEW_SHA = "b" * 40
FOLDER = "apps"
URL = f"github://nepthar/harbor/main/{FOLDER}"

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
    self.blobs: dict[str, bytes] = {}  # keyed by path within the folder
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
    self.add("hello-world.happ/manifest.toml", MANIFEST)
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
      prefix = f"{FOLDER}/"
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


MD_HAPP = b"""\
# Solo

```toml happ_path="manifest.toml"
[app]
version = "0.1.0"
display_name = "Solo"

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "echo solo"]
restart = "no"
```
"""


@pytest.fixture
def github(monkeypatch):
  fake = FakeGithub()
  fake.start()
  monkeypatch.setattr(github_lib, "API_ROOT", f"http://127.0.0.1:{fake.port}/api")
  monkeypatch.setattr(github_lib, "RAW_ROOT", f"http://127.0.0.1:{fake.port}/raw")
  yield fake
  fake.stop()


@pytest.fixture
def ctx(harbor_env) -> HarborCtx:
  return HarborCtx(load_config_file(harbor_env.config))


def a_repo(ctx, name: str = "up", ref: str = "main") -> Repo:
  return Repo(
    name=name,
    path=ctx.config.repos_root / name,
    kind="github",
    remote=parse_github_url(f"github://nepthar/harbor/{ref}/{FOLDER}"),
  )


def sizes(*paths: str) -> dict[str, int]:
  return dict.fromkeys(paths, 1)


# --- addressing ------------------------------------------------------------


def test_a_url_reads_repo_coordinates():
  folder = parse_github_url("github://nepthar/harbor/main/apps")
  assert (folder.user, folder.repo, folder.ref) == ("nepthar", "harbor", "main")
  assert folder.path == ("apps",)
  assert folder.repo_path == "apps"


def test_a_folder_is_optional_and_means_the_repository_root():
  folder = parse_github_url("github://nepthar/happs/main")
  assert folder.path == ()
  assert folder.repo_path == ""


def test_a_url_round_trips():
  url = "github://nepthar/harbor/v1.2/deep/apps"
  assert parse_github_url(url).url == url


@pytest.mark.parametrize(
  "raw",
  [
    "nepthar/harbor/main/apps",
    "github:nepthar/harbor/main/apps",
    "https://github.com/nepthar/harbor",
    "",
  ],
)
def test_only_github_urls_are_accepted(raw):
  with pytest.raises(ValueError, match="repo url"):
    parse_github_url(raw)


@pytest.mark.parametrize("raw", ["github://nepthar", "github://nepthar/harbor"])
def test_a_url_must_name_user_repo_and_ref(raw):
  with pytest.raises(ValueError, match="Malformed repo url"):
    parse_github_url(raw)


def test_an_empty_ref_is_refused():
  with pytest.raises(ValueError, match="empty ref"):
    parse_github_url("github://nepthar/harbor//apps")


@pytest.mark.parametrize("segment", ["..", "."])
def test_path_traversal_is_refused_in_a_url(segment):
  with pytest.raises(ValueError, match="Malformed path segment"):
    parse_github_url(f"github://nepthar/harbor/main/{segment}")


@pytest.mark.parametrize(
  "raw", ["github://ne pthar/harbor/main", "github://nepthar/har bor/main"]
)
def test_github_names_are_validated(raw):
  with pytest.raises(ValueError):
    parse_github_url(raw)


def test_a_name_is_taken_from_the_repository():
  assert name_from_url("github://nepthar/harbor/main/apps") == "harbor"


def test_a_repository_name_harbor_cannot_use_says_to_pass_one():
  with pytest.raises(ValueError, match="--name"):
    name_from_url("github://nepthar/harbor.js/main")


# --- picking happs out of a listing ----------------------------------------


def test_a_folder_of_happs_is_grouped_by_bundle():
  happs = group_happs(
    sizes(
      "hello-world.happ/manifest.toml",
      "hello-world.happ/go.sh",
      "solo.happ.md",
    )
  )
  assert [h.app_id for h in happs] == ["hello-world", "solo"]
  assert happs[0].files == ("hello-world.happ/go.sh", "hello-world.happ/manifest.toml")
  assert happs[1].files == ("solo.happ.md",)


def test_everything_that_is_not_a_happ_is_skipped():
  happs = group_happs(sizes("README.md", "LICENSE", "docs/guide.md", "a.happ.md"))
  assert [h.app_id for h in happs] == ["a"]


def test_a_directory_without_a_manifest_is_not_a_happ():
  happs = group_happs(sizes("stray.happ/notes.txt", "real.happ/manifest.toml"))
  assert [h.app_id for h in happs] == ["real"]


def test_a_reverse_fqdn_name_keeps_its_dots():
  happs = group_happs(sizes("io.nthr.jrnl.happ/manifest.toml"))
  assert happs[0].app_id == "io.nthr.jrnl"


def test_a_bare_suffix_does_not_name_a_happ():
  assert group_happs(sizes(".happ.md")) == ()


def test_an_unusable_app_id_is_refused():
  with pytest.raises(ValueError, match="valid app id"):
    group_happs(sizes("not a name.happ.md"))


# --- transport -------------------------------------------------------------


def test_a_branch_is_resolved_to_a_commit_sha(github):
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  assert resolve_ref(folder) == SHA


def test_a_pinned_sha_costs_no_api_call(github):
  folder = parse_github_url(f"github://nepthar/harbor/{NEW_SHA}/{FOLDER}")
  assert resolve_ref(folder) == NEW_SHA
  assert github.api_calls == []


def test_listing_returns_blobs_and_skips_directories(github):
  github.hello_world()
  github.extra_entries.append({"path": "sub", "mode": "040000", "type": "tree"})
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  entries = list_tree(folder, SHA)
  assert [e.path for e in entries] == ["hello-world.happ/manifest.toml"]


@pytest.mark.parametrize("mode,kind", [("120000", "symlink"), ("160000", "submodule")])
def test_symlinks_and_submodules_are_refused(github, mode, kind):
  github.hello_world()
  github.extra_entries.append({"path": "link", "mode": mode, "type": "blob", "size": 1})
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError, match="symlinks and submodules"):
    list_tree(folder, SHA)


def test_a_truncated_listing_is_refused(github):
  github.hello_world()
  github.truncated = True
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError, match="too large to list"):
    list_tree(folder, SHA)


@pytest.mark.parametrize("path", ["../escape", "/abs", "a/../../b"])
def test_paths_escaping_the_folder_are_refused(github, path):
  github.hello_world()
  github.extra_entries.append(
    {"path": path, "mode": "100644", "type": "blob", "size": 1}
  )
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError):
    list_tree(folder, SHA)


def test_an_oversized_file_is_refused_before_download(github):
  github.hello_world()
  github.sizes["hello-world.happ/manifest.toml"] = MAX_FILE_BYTES + 1
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError, match="per-file limit"):
    list_tree(folder, SHA)


def test_an_exhausted_rate_limit_is_named_as_such(github):
  github.commit_status = 403
  github.ratelimit_remaining = "0"
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError, match="rate limit"):
    resolve_ref(folder)


def test_a_forbidden_request_is_not_reported_as_a_rate_limit(github):
  github.commit_status = 403
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError, match="HTTP 403"):
    resolve_ref(folder)


def test_a_missing_folder_reports_not_found(github):
  github.tree_status = 404
  folder = parse_github_url(f"github://nepthar/harbor/main/{FOLDER}")
  with pytest.raises(ValueError, match="Not found"):
    list_tree(folder, SHA)


# --- mirroring -------------------------------------------------------------


def test_a_mirror_lands_every_happ_in_the_folder(github, ctx):
  github.hello_world()
  github.add("solo.happ.md", MD_HAPP)
  result = mirror(a_repo(ctx), ctx)

  assert result.happs == ("hello-world", "solo")
  assert result.sha == SHA
  assert result.previous_sha is None
  mirrored = ctx.config.repos_root / "up"
  assert (mirrored / "hello-world.happ" / "manifest.toml").read_bytes() == MANIFEST
  assert (mirrored / "solo.happ.md").read_bytes() == MD_HAPP


def test_a_mirror_costs_two_api_calls_whatever_the_app_count(github, ctx):
  github.hello_world()
  for n in range(5):
    github.add(f"app{n}.happ.md", MD_HAPP)
  mirror(a_repo(ctx), ctx)
  assert len(github.api_calls) == 2


def test_the_executable_bit_survives_a_mirror(github, ctx):
  github.add("hello-world.happ/manifest.toml", MANIFEST)
  github.add("hello-world.happ/go.sh", b"#!/bin/sh\n", mode="100755")
  mirror(a_repo(ctx), ctx)
  script = ctx.config.repos_root / "up" / "hello-world.happ" / "go.sh"
  assert script.stat().st_mode & 0o111


def test_a_second_mirror_replaces_what_the_first_left(github, ctx):
  github.hello_world()
  github.add("gone.happ.md", MD_HAPP)
  mirror(a_repo(ctx), ctx)

  del github.blobs["gone.happ.md"]
  github.sha = NEW_SHA
  result = mirror(a_repo(ctx), ctx)

  assert result.previous_sha == SHA
  assert not result.unchanged
  assert not (ctx.config.repos_root / "up" / "gone.happ.md").exists()


def test_an_unchanged_remote_is_reported_as_such(github, ctx):
  github.hello_world()
  mirror(a_repo(ctx), ctx)
  assert mirror(a_repo(ctx), ctx).unchanged


def test_a_failed_mirror_leaves_the_previous_copy_alone(github, ctx):
  github.hello_world()
  mirror(a_repo(ctx), ctx)

  github.sha = NEW_SHA
  github.tree_status = 500
  with pytest.raises(ValueError):
    mirror(a_repo(ctx), ctx)

  mirrored = ctx.config.repos_root / "up"
  assert (mirrored / "hello-world.happ" / "manifest.toml").read_bytes() == MANIFEST
  assert list(mirrored.parent.glob(".update-*")) == []
  assert ctx.harbor_db.get_repo_state("up")["sha"] == SHA


def test_a_mirrored_happ_is_in_the_catalog(github, ctx, harbor_env):
  github.hello_world()
  mirror(a_repo(ctx), ctx)
  harbor_env.config.write_text(
    f'{harbor_env.config.read_text()}\n[[repo]]\nname = "up"\nurl = "{URL}"\n'
  )
  fresh = HarborCtx(load_config_file(harbor_env.config))
  assert "hello-world" in fresh.app_catalog()
  assert fresh.app_catalog()["hello-world"][0].source == "up"


def test_a_local_repo_cannot_be_updated(ctx):
  local = Repo("dev", ctx.config.repos_root / "dev", "local")
  with pytest.raises(ValueError, match="local directory"):
    mirror(local, ctx)


def test_too_many_happs_are_refused(github, ctx):
  for n in range(MAX_HAPPS + 1):
    github.add(f"app{n}.happ.md", MD_HAPP)
  with pytest.raises(ValueError, match=f"over the {MAX_HAPPS} limit"):
    mirror(a_repo(ctx), ctx)


def test_too_many_files_are_refused(github, ctx):
  for n in range(MAX_REPO_FILES + 1):
    github.add(f"big.happ/f{n}.txt", b"x")
  github.add("big.happ/manifest.toml", MANIFEST)
  with pytest.raises(ValueError, match=f"over the {MAX_REPO_FILES} limit"):
    mirror(a_repo(ctx), ctx)


def test_an_oversized_repo_is_refused(github, ctx):
  # Each file is within the per-file limit; together they are not.
  each = MAX_FILE_BYTES
  for n in range(MAX_REPO_BYTES // each + 1):
    github.add(f"app{n}.happ.md", MD_HAPP)
    github.sizes[f"app{n}.happ.md"] = each
  with pytest.raises(ValueError, match="limit for one repo"):
    mirror(a_repo(ctx), ctx)
