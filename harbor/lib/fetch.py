"""Install a happ by copying it out of a GitHub repository.

`harbor fetch github:<user>/<repo>/<ref>/<path>/<name>.happ` downloads a happ
folder and installs it as `apps/<app_id>.happ`. A `<name>.happ.md` target does
the same for a single-file markdown happ. There is no archive, no packaging
step, and nothing for a publisher to build: committing the `.happ` directory
(or the `.happ.md` file) *is* publishing it.

Two API calls do the work. The ref is resolved to a commit sha, then one
recursive tree listing enumerates the folder at that sha. Every file is then
pulled from `raw.githubusercontent.com`, which has its own CDN quota and so
costs nothing against the API rate limit. Pinning a sha up front means a branch
moving mid-fetch cannot mix files from two commits, and gives us an exact
revision to report. A `.happ.md` is one blob, so it skips the listing and costs
only the ref resolution.

The listing carries each entry's mode and size, so hostile or oversized trees
are rejected before a single byte is downloaded.

Harbor makes no claim about *who* wrote a happ. What protects the operator is
that a manifest is short and readable: the caller shows its capability receipt
and asks before installing.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

from harbor.lib.apps import AppID
from harbor.lib.happ import HAPP_MD_CUTOFF_KB
from harbor.lib.util import fmt_size, validate_github_segment

KB = 1024
MB = 1024 * KB

API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"
API_VERSION = "2022-11-28"

GITHUB_PREFIX = "github:"
HAPP_SUFFIX = ".happ"
HAPP_MD_SUFFIX = ".happ.md"

MAX_FILES = 64
MAX_FILE_BYTES = 2 * MB
MAX_TOTAL_BYTES = 16 * MB

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
CHUNK = 64 * KB

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")

# Git modes we accept. Everything else -- notably 120000 (symlink) and 160000
# (submodule) -- is refused: a symlink could point anywhere on the host, and a
# submodule is a pointer to a repository we are not fetching.
MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_DIR = "040000"
ALLOWED_MODES = frozenset({MODE_FILE, MODE_EXEC, MODE_DIR})

USAGE = (
  f"{GITHUB_PREFIX}<user>/<repo>/<ref>/<path>/<name>{HAPP_SUFFIX} "
  f"(or <name>{HAPP_MD_SUFFIX})\n"
  f"  e.g. {GITHUB_PREFIX}nepthar/harbor/main/examples/hello-world{HAPP_SUFFIX}\n"
  f"  <ref> is a branch, tag, or full commit sha."
)


@dataclass(frozen=True)
class GithubTarget:
  """A happ inside a GitHub repository: a `.happ` directory or `.happ.md` file."""

  user: str
  repo: str
  ref: str
  path: tuple[str, ...]  # segments from the repo root to the happ
  display: str  # what the user typed, for messages

  @property
  def suffix(self) -> str:
    return HAPP_MD_SUFFIX if self.path[-1].endswith(HAPP_MD_SUFFIX) else HAPP_SUFFIX

  @property
  def is_single_file(self) -> bool:
    return self.suffix == HAPP_MD_SUFFIX

  @property
  def app_id(self) -> AppID:
    """The id this happ will install as, taken from the last segment's name.

    Same rule as a local happ, where the id is the bundle name minus its
    flavor suffix (see `app_id_from_path`). The manifest's `[app].app_id`, if
    it declares one, is cross-checked later when the staged bundle is parsed.
    """
    return AppID(self.path[-1][: -len(self.suffix)])

  @property
  def repo_path(self) -> str:
    return "/".join(self.path)

  def describe(self, sha: str) -> str:
    return f"{self.user}/{self.repo}@{sha[:8]} {self.repo_path}"


@dataclass(frozen=True)
class TreeEntry:
  """One blob in the happ directory, as reported by the tree listing."""

  path: str  # relative to the happ directory
  mode: str
  size: int

  @property
  def executable(self) -> bool:
    return self.mode == MODE_EXEC


@dataclass(frozen=True)
class FetchedHapp:
  """A downloaded happ waiting to be committed into `apps/`.

  `root` is a throwaway parent directory; `path` is the `<app_id>.happ` folder
  inside it. The nesting is what lets the staged tree be loaded by the ordinary
  `app_stack()` path, which takes an app's id from its directory name.
  """

  root: Path
  path: Path
  app_id: AppID
  sha: str
  files: int
  total_bytes: int

  @property
  def suffix(self) -> str:
    return self.path.name[len(str(self.app_id)) :]


# --- addressing ------------------------------------------------------------


def parse_target(raw: str) -> GithubTarget:
  """Parse a `github:` target into the repository coordinates it names."""
  if not raw.startswith(GITHUB_PREFIX):
    raise ValueError(f"Unsupported fetch target {raw!r}; expected\n  {USAGE}")

  parts = raw[len(GITHUB_PREFIX) :].split("/")
  if len(parts) < 4:
    raise ValueError(f"Malformed GitHub target {raw!r}; expected\n  {USAGE}")

  user, repo, ref, *path = parts
  user = validate_github_segment(user, "user")
  repo = validate_github_segment(repo, "repo")
  if not ref:
    raise ValueError(f"Malformed GitHub target {raw!r}: empty ref")
  for segment in path:
    _check_segment(segment, raw)

  last = path[-1]
  bare = last in (HAPP_SUFFIX, HAPP_MD_SUFFIX)
  if not last.endswith((HAPP_SUFFIX, HAPP_MD_SUFFIX)) or bare:
    raise ValueError(
      f"{raw} does not name a happ: the last path segment must be "
      f"<name>{HAPP_SUFFIX} or <name>{HAPP_MD_SUFFIX}, and harbor takes the "
      f"app id from it."
    )

  return GithubTarget(user=user, repo=repo, ref=ref, path=tuple(path), display=raw)


def _check_segment(segment: str, context: str) -> None:
  if segment in ("", ".", ".."):
    raise ValueError(f"Malformed path segment {segment!r} in {context}")
  if any(c < " " or c == "\x7f" for c in segment):
    raise ValueError(f"Unprintable character in path segment of {context}")


# --- transport -------------------------------------------------------------


def _headers(accept: str) -> dict[str, str]:
  headers = {
    "Accept": accept,
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": "harbor",
  }
  token = os.environ.get("GITHUB_TOKEN")
  if token:
    headers["Authorization"] = f"Bearer {token}"
  return headers


def _rate_limited(resp: requests.Response) -> bool:
  """Distinguish an exhausted quota from an ordinary 403.

  429 is always rate limiting. A 403 is only rate limiting when GitHub says the
  remaining budget is zero -- it is also what a blocked or forbidden request
  returns, and reporting that as a rate limit would send the operator chasing
  the wrong fix.
  """
  if resp.status_code == 429:
    return True
  return resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"


def _get(url: str, *, accept: str, stream: bool = False) -> requests.Response:
  try:
    resp = requests.get(
      url,
      headers=_headers(accept),
      stream=stream,
      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
  except requests.RequestException as e:
    raise ValueError(f"Could not reach {url}: {e}") from e

  if resp.status_code == 200:
    return resp

  detail = _api_message(resp)
  resp.close()

  if resp.status_code == 404:
    raise ValueError(
      f"Not found: {url}\n"
      f"Check the user, repo, ref, and path -- a private repository also "
      f"reports 404 unless GITHUB_TOKEN is set."
    )
  if _rate_limited(resp):
    raise ValueError(
      "GitHub rate limit reached (60 requests/hour per IP when "
      "unauthenticated, and a fetch spends two).\n"
      "Set GITHUB_TOKEN to raise it to 5000/hour, or wait and retry."
    )
  raise ValueError(f"{url} returned HTTP {resp.status_code}{detail}")


def _api_message(resp: requests.Response) -> str:
  """GitHub's own explanation of a failure, when it sent one."""
  try:
    message = resp.json().get("message")
  except ValueError:
    return ""
  return f": {message}" if message else ""


def resolve_ref(target: GithubTarget) -> str:
  """Resolve a branch, tag, or sha to the full commit sha it names.

  A branch is a moving target: listing the tree and downloading the files at a
  pinned sha keeps a fetch from straddling two commits. An already-pinned sha
  skips the call, so pinning also costs less rate limit.
  """
  if _SHA_RE.fullmatch(target.ref):
    return target.ref

  url = f"{API_ROOT}/repos/{target.user}/{target.repo}/commits/{quote(target.ref)}"
  resp = _get(url, accept="application/vnd.github.sha")
  with resp:
    sha = resp.text.strip()

  if not _SHA_RE.fullmatch(sha):
    raise ValueError(f"{url} did not return a commit sha (got {sha[:64]!r})")
  return sha


def list_tree(target: GithubTarget, sha: str) -> tuple[TreeEntry, ...]:
  """List the happ directory at `sha`, rejecting anything unsafe to unpack."""
  ref_path = quote(f"{sha}:{target.repo_path}", safe=":/")
  url = f"{API_ROOT}/repos/{target.user}/{target.repo}/git/trees/{ref_path}?recursive=1"
  resp = _get(url, accept="application/vnd.github+json")
  with resp:
    try:
      payload = resp.json()
    except ValueError as e:
      raise ValueError(f"{url} did not return JSON") from e

  if payload.get("truncated"):
    raise ValueError(
      f"{target.display} is too large to list in one request. "
      f"Harbor only distributes small happs today."
    )
  return _check_entries(payload.get("tree") or [], target)


def _check_entries(tree: list[dict], target: GithubTarget) -> tuple[TreeEntry, ...]:
  entries: list[TreeEntry] = []
  total = 0

  for item in tree:
    path = str(item.get("path", ""))
    mode = str(item.get("mode", ""))

    if mode not in ALLOWED_MODES:
      raise ValueError(
        f"{target.display} contains {path!r}, which is not a regular file or "
        f"directory (mode {mode}). Harbor refuses symlinks and submodules in a "
        f"fetched happ."
      )
    if not path or path.startswith("/"):
      raise ValueError(f"{target.display} contains an unsafe path: {path!r}")
    for segment in path.split("/"):
      _check_segment(segment, f"{target.display} listing")

    if mode == MODE_DIR:
      continue

    size = int(item.get("size") or 0)
    if size > MAX_FILE_BYTES:
      raise ValueError(
        f"{target.display}: {path} is {fmt_size(size)}, over the "
        f"{fmt_size(MAX_FILE_BYTES)} per-file limit."
      )
    total += size
    entries.append(TreeEntry(path=path, mode=mode, size=size))

  if len(entries) > MAX_FILES:
    raise ValueError(
      f"{target.display} holds {len(entries)} files, over the {MAX_FILES}-file limit."
    )
  if total > MAX_TOTAL_BYTES:
    raise ValueError(
      f"{target.display} is {fmt_size(total)}, over the "
      f"{fmt_size(MAX_TOTAL_BYTES)} limit for a happ."
    )
  if not any(entry.path == "manifest.toml" for entry in entries):
    raise ValueError(
      f"{target.display} has no manifest.toml, so it is not a happ directory."
    )
  return tuple(entries)


def _download(url: str, dest: Path, limit: int) -> int:
  """Stream one blob to `dest`, refusing to exceed `limit` bytes."""
  resp = _get(url, accept="application/vnd.github.raw", stream=True)
  written = 0
  with resp, dest.open("wb") as f:
    for chunk in resp.iter_content(CHUNK):
      written += len(chunk)
      if written > limit:
        raise ValueError(f"{url} sent more than the {fmt_size(limit)} limit.")
      f.write(chunk)
  return written


# --- staging and install ---------------------------------------------------


def destination_for(app_id: AppID, apps_root: Path, suffix: str = HAPP_SUFFIX) -> Path:
  """Where `app_id` will install, refusing to disturb anything already there.

  Both bundle flavors are checked: one id maps to one catalog entry, whatever
  its suffix.
  """
  for flavor in (HAPP_SUFFIX, HAPP_MD_SUFFIX):
    existing = apps_root / f"{app_id}{flavor}"
    if existing.exists():
      raise ValueError(
        f"{app_id} is already installed at {existing}.\n"
        f"Remove it first if you mean to replace it; harbor fetch never "
        f"overwrites an installed happ."
      )
  return apps_root / f"{app_id}{suffix}"


def _staging_root(app_id: AppID, apps_root: Path) -> Path:
  """A fresh dotted scratch dir beside the destination.

  Dotted so a partially written bundle is never picked up by `known_bundles()`
  (which globs `*.happ` / `*.happ.md`), and a sibling so the final move is a
  rename within one filesystem rather than a copy.
  """
  apps_root.mkdir(parents=True, exist_ok=True)
  root = apps_root / f".fetch-{app_id}-{os.getpid()}"
  shutil.rmtree(root, ignore_errors=True)
  return root


def _raw_url(target: GithubTarget, sha: str, *extra: str) -> str:
  return "/".join(
    (
      RAW_ROOT,
      quote(target.user),
      quote(target.repo),
      sha,
      *(quote(segment) for segment in target.path),
      *(quote(segment) for segment in extra),
    )
  )


def stage_md_happ(target: GithubTarget, apps_root: Path) -> FetchedHapp:
  """Download a single-file `.happ.md` into a staging directory.

  One blob, so there is no tree listing to vet: the stream cap enforces the
  markdown size limit, and the caller's parse (`load_happ`) is the content
  check, exactly as it is for a local `.happ.md`.
  """
  sha = resolve_ref(target)
  app_id = target.app_id
  root = _staging_root(app_id, apps_root)
  bundle = root / f"{app_id}{HAPP_MD_SUFFIX}"
  bundle.parent.mkdir(parents=True)

  try:
    total = _download(_raw_url(target, sha), bundle, HAPP_MD_CUTOFF_KB * KB)
    bundle.chmod(0o644)
  except BaseException:
    shutil.rmtree(root, ignore_errors=True)
    raise

  return FetchedHapp(
    root=root,
    path=bundle,
    app_id=app_id,
    sha=sha,
    files=1,
    total_bytes=total,
  )


def stage_happ(target: GithubTarget, apps_root: Path) -> FetchedHapp:
  """Download the happ into a staging directory beside its final home."""
  if target.is_single_file:
    return stage_md_happ(target, apps_root)

  sha = resolve_ref(target)
  entries = list_tree(target, sha)

  app_id = target.app_id
  root = _staging_root(app_id, apps_root)
  bundle = root / f"{app_id}{HAPP_SUFFIX}"
  bundle.mkdir(parents=True)

  total = 0
  try:
    for entry in entries:
      url = _raw_url(target, sha, *entry.path.split("/"))
      dest = bundle / entry.path
      dest.parent.mkdir(parents=True, exist_ok=True)
      total += _download(url, dest, MAX_FILE_BYTES)
      # Preserve the executable bit: happs ship scripts that run in-container,
      # and losing +x would fail only at runtime.
      dest.chmod(0o755 if entry.executable else 0o644)
  except BaseException:
    shutil.rmtree(root, ignore_errors=True)
    raise

  return FetchedHapp(
    root=root,
    path=bundle,
    app_id=app_id,
    sha=sha,
    files=len(entries),
    total_bytes=total,
  )


def commit_happ(staged: FetchedHapp, apps_root: Path) -> Path:
  """Move a staged happ into `apps/` as a single rename."""
  dest = destination_for(staged.app_id, apps_root, staged.suffix)
  try:
    os.replace(staged.path, dest)
  except OSError as e:
    raise ValueError(f"Could not install {staged.app_id} at {dest}: {e}") from e
  finally:
    shutil.rmtree(staged.root, ignore_errors=True)
  return dest


def discard(staged: FetchedHapp) -> None:
  shutil.rmtree(staged.root, ignore_errors=True)
