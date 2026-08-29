"""Read files out of a GitHub repository, safely.

Resolving a ref and listing a tree are API calls, and cost two per mirror
whatever the file count; blobs come from raw.githubusercontent.com, which is
not on the API quota.

Nothing here knows what a happ or a repo is; see `harbor.lib.repo`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

from harbor.lib.repo import GithubFolder, check_segment
from harbor.lib.util import fmt_size

KB = 1024
MB = 1024 * KB

API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"
API_VERSION = "2022-11-28"

# Per happ, as before. A repo is bounded separately in `harbor.lib.repo`.
MAX_FILE_BYTES = 2 * MB

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
CHUNK = 64 * KB

# Git modes we accept. Everything else -- notably 120000 (symlink) and 160000
# (submodule) -- is refused: a symlink could point anywhere on the host, and a
# submodule is a pointer to a repository we are not fetching.
MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_DIR = "040000"
ALLOWED_MODES = frozenset({MODE_FILE, MODE_EXEC, MODE_DIR})

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class TreeEntry:
  """One blob under the listed folder, its path relative to that folder."""

  path: str
  mode: str
  size: int

  @property
  def executable(self) -> bool:
    return self.mode == MODE_EXEC


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
  """Distinguish an exhausted quota from an ordinary 403."""
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


def resolve_ref(folder: GithubFolder) -> str:
  """Resolve a branch, tag, or sha to the full commit sha it names."""
  if _SHA_RE.fullmatch(folder.ref):
    return folder.ref

  url = f"{API_ROOT}/repos/{folder.user}/{folder.repo}/commits/{quote(folder.ref)}"
  resp = _get(url, accept="application/vnd.github.sha")
  with resp:
    sha = resp.text.strip()

  if not _SHA_RE.fullmatch(sha):
    raise ValueError(f"{url} did not return a commit sha (got {sha[:64]!r})")
  return sha


def list_tree(folder: GithubFolder, sha: str) -> tuple[TreeEntry, ...]:
  """List every file under the folder at `sha`, refusing anything unsafe."""
  # `sha:path` addresses a subtree; a bare sha is the repository root.
  ref_path = quote(f"{sha}:{folder.repo_path}" if folder.path else sha, safe=":/")
  url = f"{API_ROOT}/repos/{folder.user}/{folder.repo}/git/trees/{ref_path}?recursive=1"
  resp = _get(url, accept="application/vnd.github+json")
  with resp:
    try:
      payload = resp.json()
    except ValueError as e:
      raise ValueError(f"{url} did not return JSON") from e

  if payload.get("truncated"):
    raise ValueError(
      f"{folder.url} is too large to list in one request. Harbor only "
      f"distributes small happs today."
    )
  return _check_entries(payload.get("tree") or [], folder)


def _check_entries(tree: list[dict], folder: GithubFolder) -> tuple[TreeEntry, ...]:
  entries: list[TreeEntry] = []

  for item in tree:
    path = str(item.get("path", ""))
    mode = str(item.get("mode", ""))

    if mode not in ALLOWED_MODES:
      raise ValueError(
        f"{folder.url} contains {path!r}, which is not a regular file or "
        f"directory (mode {mode}). Harbor refuses symlinks and submodules in a "
        f"mirrored repo."
      )
    if not path or path.startswith("/"):
      raise ValueError(f"{folder.url} contains an unsafe path: {path!r}")
    for segment in path.split("/"):
      check_segment(segment, f"{folder.url} listing")

    if mode == MODE_DIR:
      continue

    size = int(item.get("size") or 0)
    if size > MAX_FILE_BYTES:
      raise ValueError(
        f"{folder.url}: {path} is {fmt_size(size)}, over the "
        f"{fmt_size(MAX_FILE_BYTES)} per-file limit."
      )
    entries.append(TreeEntry(path=path, mode=mode, size=size))

  return tuple(entries)


def raw_url(folder: GithubFolder, sha: str, *extra: str) -> str:
  return "/".join(
    (
      RAW_ROOT,
      quote(folder.user),
      quote(folder.repo),
      sha,
      *(quote(segment) for segment in folder.path),
      *(quote(segment) for segment in extra),
    )
  )


def download(url: str, dest: Path, limit: int = MAX_FILE_BYTES) -> int:
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
