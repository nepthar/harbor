"""Tests for application repos: addressing, mirroring, and what a mirror holds.

Every test runs against a local fake of the two GitHub endpoints kelso uses,
so the suite never touches the network. `kelso.lib.github.API_ROOT` and
`RAW_ROOT` are the only seams needed for that.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import pytest

from kelso.jobs.repo import RepoAddJob, RepoRemoveJob, RepoUpdateJob
from kelso.lib import github as github_lib
from kelso.lib import repo as repo_lib
from kelso.lib.config import load_config_file
from kelso.lib.github import MAX_FILE_BYTES, list_tree, resolve_ref
from kelso.lib.kelso import KelsoCtx
from kelso.lib.repo import (
  MAX_BUNDLES,
  MAX_REPO_BYTES,
  MAX_REPO_FILES,
  Repo,
  group_bundles,
  mirror,
  name_from_url,
  parse_github_url,
)

SHA = "a1b2c3d4" * 5  # 40 hex chars
NEW_SHA = "b" * 40
FOLDER = "apps"
URL = f"github://nepthar/kelso/main/{FOLDER}"

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
cmd     = ["/bin/sh", "-c", "/kelso/app/go.sh"]
volumes = { app = "/kelso/app" }
restart = "no"
"""


# --- fake github -----------------------------------------------------------


class FakeGithub:
  """The two endpoints kelso talks to, backed by an in-memory repo."""

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
    self.add("hello-world.klso/manifest.toml", MANIFEST)
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


MD_BUNDLE = b"""\
# Solo

```toml klso_path="manifest.toml"
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
def ctx(kelso_env) -> KelsoCtx:
  return KelsoCtx(load_config_file(kelso_env.config))


def a_repo(ctx, name: str = "up", ref: str = "main") -> Repo:
  return Repo(
    name=name,
    path=ctx.config.repos_root / name,
    kind="github",
    remote=parse_github_url(f"github://nepthar/kelso/{ref}/{FOLDER}"),
  )


def sizes(*paths: str) -> dict[str, int]:
  return dict.fromkeys(paths, 1)


# --- addressing ------------------------------------------------------------


def test_a_url_reads_repo_coordinates():
  folder = parse_github_url("github://nepthar/kelso/main/apps")
  assert (folder.user, folder.repo, folder.ref) == ("nepthar", "kelso", "main")
  assert folder.path == ("apps",)
  assert folder.repo_path == "apps"


def test_a_folder_is_optional_and_means_the_repository_root():
  folder = parse_github_url("github://nepthar/bundles/main")
  assert folder.path == ()
  assert folder.repo_path == ""


def test_a_url_round_trips():
  url = "github://nepthar/kelso/v1.2/deep/apps"
  assert parse_github_url(url).url == url


@pytest.mark.parametrize(
  "raw",
  [
    "nepthar/kelso/main/apps",
    "github:nepthar/kelso/main/apps",
    "https://github.com/nepthar/kelso",
    "",
  ],
)
def test_only_github_urls_are_accepted(raw):
  with pytest.raises(ValueError, match="repo url"):
    parse_github_url(raw)


@pytest.mark.parametrize("raw", ["github://nepthar", "github://nepthar/kelso"])
def test_a_url_must_name_user_repo_and_ref(raw):
  with pytest.raises(ValueError, match="Malformed repo url"):
    parse_github_url(raw)


def test_an_empty_ref_is_refused():
  with pytest.raises(ValueError, match="empty ref"):
    parse_github_url("github://nepthar/kelso//apps")


@pytest.mark.parametrize("segment", ["..", "."])
def test_path_traversal_is_refused_in_a_url(segment):
  with pytest.raises(ValueError, match="Malformed path segment"):
    parse_github_url(f"github://nepthar/kelso/main/{segment}")


@pytest.mark.parametrize(
  "raw", ["github://ne pthar/kelso/main", "github://nepthar/har bor/main"]
)
def test_github_names_are_validated(raw):
  with pytest.raises(ValueError):
    parse_github_url(raw)


def test_a_name_is_taken_from_the_repository():
  assert name_from_url("github://nepthar/kelso/main/apps") == "kelso"


def test_a_repository_name_kelso_cannot_use_says_to_pass_one():
  with pytest.raises(ValueError, match="--name"):
    name_from_url("github://nepthar/kelso.js/main")


# --- picking bundles out of a listing ----------------------------------------


def test_a_folder_of_bundles_is_grouped_by_bundle():
  bundles = group_bundles(
    sizes(
      "hello-world.klso/manifest.toml",
      "hello-world.klso/go.sh",
      "solo.klso.md",
    )
  )
  assert [h.app_id for h in bundles] == ["hello-world", "solo"]
  assert bundles[0].files == (
    "hello-world.klso/go.sh",
    "hello-world.klso/manifest.toml",
  )
  assert bundles[1].files == ("solo.klso.md",)


def test_everything_that_is_not_a_bundle_is_skipped():
  bundles = group_bundles(sizes("README.md", "LICENSE", "docs/guide.md", "a.klso.md"))
  assert [h.app_id for h in bundles] == ["a"]


def test_a_directory_without_a_manifest_is_not_a_bundle():
  bundles = group_bundles(sizes("stray.klso/notes.txt", "real.klso/manifest.toml"))
  assert [h.app_id for h in bundles] == ["real"]


def test_a_reverse_fqdn_name_keeps_its_dots():
  bundles = group_bundles(sizes("io.nthr.jrnl.klso/manifest.toml"))
  assert bundles[0].app_id == "io.nthr.jrnl"


def test_a_bare_suffix_does_not_name_a_bundle():
  assert group_bundles(sizes(".klso.md")) == ()


def test_an_unusable_app_id_is_refused():
  with pytest.raises(ValueError, match="valid app id"):
    group_bundles(sizes("not a name.klso.md"))


# --- transport -------------------------------------------------------------


def test_a_branch_is_resolved_to_a_commit_sha(github):
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  assert resolve_ref(folder) == SHA


def test_a_pinned_sha_costs_no_api_call(github):
  folder = parse_github_url(f"github://nepthar/kelso/{NEW_SHA}/{FOLDER}")
  assert resolve_ref(folder) == NEW_SHA
  assert github.api_calls == []


def test_listing_returns_blobs_and_skips_directories(github):
  github.hello_world()
  github.extra_entries.append({"path": "sub", "mode": "040000", "type": "tree"})
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  entries = list_tree(folder, SHA)
  assert [e.path for e in entries] == ["hello-world.klso/manifest.toml"]


@pytest.mark.parametrize("mode,kind", [("120000", "symlink"), ("160000", "submodule")])
def test_symlinks_and_submodules_are_refused(github, mode, kind):
  github.hello_world()
  github.extra_entries.append({"path": "link", "mode": mode, "type": "blob", "size": 1})
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError, match="symlinks and submodules"):
    list_tree(folder, SHA)


def test_a_truncated_listing_is_refused(github):
  github.hello_world()
  github.truncated = True
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError, match="too large to list"):
    list_tree(folder, SHA)


@pytest.mark.parametrize("path", ["../escape", "/abs", "a/../../b"])
def test_paths_escaping_the_folder_are_refused(github, path):
  github.hello_world()
  github.extra_entries.append(
    {"path": path, "mode": "100644", "type": "blob", "size": 1}
  )
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError):
    list_tree(folder, SHA)


def test_an_oversized_file_is_refused_before_download(github):
  github.hello_world()
  github.sizes["hello-world.klso/manifest.toml"] = MAX_FILE_BYTES + 1
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError, match="per-file limit"):
    list_tree(folder, SHA)


def test_an_exhausted_rate_limit_is_named_as_such(github):
  github.commit_status = 403
  github.ratelimit_remaining = "0"
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError, match="rate limit"):
    resolve_ref(folder)


def test_a_forbidden_request_is_not_reported_as_a_rate_limit(github):
  github.commit_status = 403
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError, match="HTTP 403"):
    resolve_ref(folder)


def test_a_missing_folder_reports_not_found(github):
  github.tree_status = 404
  folder = parse_github_url(f"github://nepthar/kelso/main/{FOLDER}")
  with pytest.raises(ValueError, match="Not found"):
    list_tree(folder, SHA)


# --- mirroring -------------------------------------------------------------


def test_a_mirror_lands_every_bundle_in_the_folder(github, ctx):
  github.hello_world()
  github.add("solo.klso.md", MD_BUNDLE)
  result = mirror(a_repo(ctx), ctx)

  assert result.bundles == ("hello-world", "solo")
  assert result.sha == SHA
  assert result.previous_sha is None
  mirrored = ctx.config.repos_root / "up"
  assert (mirrored / "hello-world.klso" / "manifest.toml").read_bytes() == MANIFEST
  assert (mirrored / "solo.klso.md").read_bytes() == MD_BUNDLE


def test_a_mirror_costs_two_api_calls_whatever_the_app_count(github, ctx):
  github.hello_world()
  for n in range(5):
    github.add(f"app{n}.klso.md", MD_BUNDLE)
  mirror(a_repo(ctx), ctx)
  assert len(github.api_calls) == 2


def test_the_executable_bit_survives_a_mirror(github, ctx):
  github.add("hello-world.klso/manifest.toml", MANIFEST)
  github.add("hello-world.klso/go.sh", b"#!/bin/sh\n", mode="100755")
  mirror(a_repo(ctx), ctx)
  script = ctx.config.repos_root / "up" / "hello-world.klso" / "go.sh"
  assert script.stat().st_mode & 0o111


def test_a_second_mirror_replaces_what_the_first_left(github, ctx):
  github.hello_world()
  github.add("gone.klso.md", MD_BUNDLE)
  mirror(a_repo(ctx), ctx)

  del github.blobs["gone.klso.md"]
  github.sha = NEW_SHA
  result = mirror(a_repo(ctx), ctx)

  assert result.previous_sha == SHA
  assert not result.unchanged
  assert not (ctx.config.repos_root / "up" / "gone.klso.md").exists()


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
  assert (mirrored / "hello-world.klso" / "manifest.toml").read_bytes() == MANIFEST
  assert list(mirrored.parent.glob(".update-*")) == []
  assert ctx.kelso_db.get_repo_state("up")["sha"] == SHA


def test_a_mirrored_bundle_is_in_the_catalog(github, ctx, kelso_env):
  github.hello_world()
  mirror(a_repo(ctx), ctx)
  kelso_env.config.write_text(
    f'{kelso_env.config.read_text()}\n[repo.up]\nurl = "{URL}"\n'
  )
  fresh = KelsoCtx(load_config_file(kelso_env.config))
  assert "hello-world" in fresh.app_catalog()
  assert fresh.app_catalog()["hello-world"][0].source == "up"


def test_a_local_repo_cannot_be_updated(ctx):
  local = Repo("dev", ctx.config.repos_root / "dev", "local")
  with pytest.raises(ValueError, match="local directory"):
    mirror(local, ctx)


def test_too_many_bundles_are_refused(github, ctx):
  for n in range(MAX_BUNDLES + 1):
    github.add(f"app{n}.klso.md", MD_BUNDLE)
  with pytest.raises(ValueError, match=f"over the {MAX_BUNDLES} limit"):
    mirror(a_repo(ctx), ctx)


def test_too_many_files_are_refused(github, ctx):
  for n in range(MAX_REPO_FILES + 1):
    github.add(f"big.klso/f{n}.txt", b"x")
  github.add("big.klso/manifest.toml", MANIFEST)
  with pytest.raises(ValueError, match=f"over the {MAX_REPO_FILES} limit"):
    mirror(a_repo(ctx), ctx)


def test_an_oversized_repo_is_refused(github, ctx):
  # Each file is within the per-file limit; together they are not.
  each = MAX_FILE_BYTES
  for n in range(MAX_REPO_BYTES // each + 1):
    github.add(f"app{n}.klso.md", MD_BUNDLE)
    github.sizes[f"app{n}.klso.md"] = each
  with pytest.raises(ValueError, match="limit for one repo"):
    mirror(a_repo(ctx), ctx)


# --- the repo verbs ---------------------------------------------------------


def a_local_repo(kelso_env, name: str = "dev"):
  path = kelso_env.root / name
  path.mkdir()
  with open(kelso_env.config, "a") as f:
    f.write(f'\n[repo.{name}]\npath = "{path}"\n')
  return path


def test_add_writes_the_repo_and_mirrors_it(github, ctx, kelso_env):
  github.hello_world()
  result = repo_lib.add(ctx, URL)

  assert result.repo.name == "kelso"
  assert result.mirrored is not None
  assert result.mirrored.bundles == ("hello-world",)
  assert "[repo.kelso]" in kelso_env.config.read_text()

  fresh = KelsoCtx(load_config_file(kelso_env.config))
  assert fresh.app_catalog()["hello-world"][0].source == "kelso"


def test_add_takes_a_name_of_its_own(github, ctx, kelso_env):
  github.hello_world()
  assert repo_lib.add(ctx, URL, name="mine").repo.name == "mine"


def test_add_refuses_a_name_already_taken(github, ctx):
  github.hello_world()
  repo_lib.add(ctx, URL)
  with pytest.raises(ValueError, match="already exists"):
    repo_lib.add(ctx, URL)


def test_a_local_repo_needs_a_name(ctx, kelso_env):
  with pytest.raises(ValueError, match="needs a name"):
    repo_lib.add(ctx, str(kelso_env.root / "somewhere"))


def test_remove_drops_the_entry_and_the_mirror(github, ctx, kelso_env):
  github.hello_world()
  repo_lib.add(ctx, URL)
  fresh = KelsoCtx(load_config_file(kelso_env.config))
  mirrored = fresh.config.repos["kelso"].path
  assert mirrored.is_dir()

  result = repo_lib.remove(fresh, "kelso")

  assert result.name == "kelso"
  assert not mirrored.exists()
  assert fresh.kelso_db.get_repo_state("kelso") is None
  assert "kelso" not in load_config_file(kelso_env.config).repos


def test_main_cannot_be_removed(ctx):
  with pytest.raises(ValueError, match="built in"):
    repo_lib.remove(ctx, "main")


def test_removing_an_unknown_repo_names_the_known_ones(ctx):
  with pytest.raises(ValueError, match="configured repos: main"):
    repo_lib.remove(ctx, "nope")


def test_update_refuses_a_local_repo_by_name(ctx, kelso_env):
  a_local_repo(kelso_env)
  fresh = KelsoCtx(load_config_file(kelso_env.config))
  with pytest.raises(ValueError, match="local directory"):
    repo_lib.update(fresh, "dev")


def test_update_with_no_name_skips_local_repos(ctx, kelso_env):
  a_local_repo(kelso_env)
  fresh = KelsoCtx(load_config_file(kelso_env.config))
  assert repo_lib.update(fresh) == ()


def test_contested_lines_name_every_repo_carrying_an_id(github, ctx, kelso_env):
  github.hello_world()
  repo_lib.add(ctx, URL)
  fresh = KelsoCtx(load_config_file(kelso_env.config))
  repo_lib.add(fresh, URL, name="mirror")
  fresh = KelsoCtx(load_config_file(kelso_env.config))

  [line] = repo_lib.contested_lines(fresh)
  assert "hello-world is in 2 repos (kelso, mirror)" in line
  assert "hello-world@<repo>" in line


# --- the repo jobs ----------------------------------------------------------


def test_repo_add_job_mirrors_the_folder(github, ctx, kelso_env):
  github.hello_world()
  RepoAddJob.call({"url": URL}, ctx)

  fresh = KelsoCtx(load_config_file(kelso_env.config))
  assert "hello-world" in fresh.app_catalog()


@pytest.mark.parametrize(
  "url", ["/etc", "~/bundles", "./apps", "apps", "https://github.com/a/b"]
)
def test_repo_add_job_takes_a_url_and_never_a_path(ctx, url):
  """Local repos are CLI-only; see the note above `runner.JOBS`."""
  with pytest.raises(ValueError, match="takes a github:// url"):
    RepoAddJob.prepare({"url": url}, ctx)


def test_repo_add_job_refuses_a_malformed_url_before_writing(ctx, kelso_env):
  before = kelso_env.config.read_text()
  with pytest.raises(ValueError, match="Malformed repo url"):
    RepoAddJob.prepare({"url": "github://nepthar"}, ctx)
  assert kelso_env.config.read_text() == before


def test_repo_update_job_brings_the_mirror_forward(github, ctx, kelso_env):
  github.hello_world()
  RepoAddJob.call({"url": URL}, ctx)

  github.add("second.klso.md", MD_BUNDLE)
  github.sha = NEW_SHA
  fresh = KelsoCtx(load_config_file(kelso_env.config))
  RepoUpdateJob.call({"name": "kelso"}, fresh)

  fresh = KelsoCtx(load_config_file(kelso_env.config))
  assert set(fresh.app_catalog()) >= {"hello-world", "second"}
  assert fresh.kelso_db.get_repo_state("kelso")["sha"] == NEW_SHA


def test_repo_update_job_refuses_an_unknown_repo(ctx):
  with pytest.raises(ValueError, match="No repo 'nope'"):
    RepoUpdateJob.prepare({"name": "nope"}, ctx)


def test_repo_remove_job_drops_it(github, ctx, kelso_env):
  github.hello_world()
  RepoAddJob.call({"url": URL}, ctx)

  fresh = KelsoCtx(load_config_file(kelso_env.config))
  RepoRemoveJob.call({"name": "kelso"}, fresh)

  assert "kelso" not in load_config_file(kelso_env.config).repos


def test_repo_remove_job_refuses_main(ctx):
  with pytest.raises(ValueError, match="built in"):
    RepoRemoveJob.call({"name": "main"}, ctx)


def test_repo_jobs_are_recorded_as_activity(github, ctx):
  github.hello_world()
  job = RepoAddJob.call({"url": URL}, ctx)

  assert job.state == "done"
  assert job.log
  assert "Mirrored 1 bundles" in (ctx.config.activity_root / job.log).read_text()


def test_a_duplicate_name_never_reaches_the_config_file(github, ctx, kelso_env):
  """The name is a table key, so a second write would overwrite the first."""
  github.hello_world()
  repo_lib.add(ctx, URL)
  written = kelso_env.config.read_text()

  # A stale ctx is the realistic case: the caller loaded config before the add.
  with pytest.raises(ValueError, match="already exists"):
    repo_lib.add(ctx, URL)

  assert kelso_env.config.read_text() == written
  assert "kelso" in load_config_file(kelso_env.config).repos


def test_main_cannot_be_shadowed_by_a_configured_repo(ctx, kelso_env):
  before = kelso_env.config.read_text()
  with pytest.raises(ValueError, match="built-in repo"):
    repo_lib.add(ctx, URL, name="main")
  assert kelso_env.config.read_text() == before
