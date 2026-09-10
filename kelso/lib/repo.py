"""Application repositories: the directories the catalog scans for bundles.

A `local` repo is a directory the operator keeps; a `github` repo is one kelso
mirrors into `repos/<name>/`. Nothing below `Repo.path` knows which kind it
has. `main` is built in at `repos/main`.

This module owns the repo model and the verbs over it. Talking to GitHub is
`kelso.lib.github`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kelso.lib.apps import AppID
from kelso.lib.bundle import KLSO_MD_SUFFIX, KLSO_SUFFIX, KLSO_TAR_SUFFIX
from kelso.lib.util import (
  fmt_size,
  now_ts,
  validate_github_segment,
  validate_identifier,
)

MAIN_REPO = "main"

GITHUB_SCHEME = "github://"

RepoKind = Literal["local", "github"]

USAGE = (
  f"{GITHUB_SCHEME}<user>/<repo>/<ref>[/<path>]\n"
  f"  e.g. {GITHUB_SCHEME}nepthar/kelso/main/apps\n"
  f"  <ref> is a branch, tag, or commit sha; <path> is the folder of bundles\n"
  f"  inside the repository, and defaults to its root."
)


@dataclass(frozen=True)
class GithubFolder:
  """The folder inside a GitHub repository that a repo mirrors."""

  user: str
  repo: str
  ref: str
  path: tuple[str, ...]

  @property
  def url(self) -> str:
    return "/".join((f"{GITHUB_SCHEME}{self.user}", self.repo, self.ref, *self.path))

  @property
  def repo_path(self) -> str:
    """The folder as git addresses it; empty means the repository root."""
    return "/".join(self.path)

  def describe(self, sha: str) -> str:
    where = f" {self.repo_path}" if self.path else ""
    return f"{self.user}/{self.repo}@{sha[:8]}{where}"


@dataclass(frozen=True)
class Repo:
  """One configured source of bundles."""

  name: str
  path: Path
  kind: RepoKind
  remote: GithubFolder | None = None

  @property
  def mirrored(self) -> bool:
    return self.remote is not None

  def describe(self) -> str:
    return self.remote.url if self.remote else str(self.path)


def parse_github_url(raw: str) -> GithubFolder:
  """Parse a `github://user/repo/ref[/path]` repo URL."""
  if not raw.startswith(GITHUB_SCHEME):
    raise ValueError(f"Unsupported repo url {raw!r}; expected\n  {USAGE}")

  parts = raw[len(GITHUB_SCHEME) :].split("/")
  if len(parts) < 3:
    raise ValueError(f"Malformed repo url {raw!r}; expected\n  {USAGE}")

  user, repo, ref, *path = parts
  user = validate_github_segment(user, "user")
  repo = validate_github_segment(repo, "repo")
  if not ref:
    raise ValueError(f"Malformed repo url {raw!r}: empty ref")
  for segment in path:
    check_segment(segment, raw)

  return GithubFolder(user=user, repo=repo, ref=ref, path=tuple(path))


def check_segment(segment: str, context: str) -> None:
  """Refuse a path segment that could escape the folder it is listed under."""
  if segment in ("", ".", ".."):
    raise ValueError(f"Malformed path segment {segment!r} in {context}")
  if any(c < " " or c == "\x7f" for c in segment):
    raise ValueError(f"Unprintable character in path segment of {context}")


def name_from_url(raw: str) -> str:
  """The repo name a url implies: the GitHub repository's own name."""
  name = parse_github_url(raw).repo
  try:
    validate_identifier(name)
  except ValueError as e:
    raise ValueError(
      f"{raw} implies the repo name {name!r}, which kelso cannot use: {e}\n"
      f"Pass --name to choose another."
    ) from e
  return name


# --- mirroring -------------------------------------------------------------

MAX_BUNDLES = 128
MAX_REPO_FILES = 1024
MAX_REPO_BYTES = 64 * 1024 * 1024

_SUFFIXES = (KLSO_TAR_SUFFIX, KLSO_MD_SUFFIX, KLSO_SUFFIX)


@dataclass(frozen=True)
class RemoteBundle:
  """One bundle in a repo listing, and the blobs that make it up."""

  app_id: str
  name: str  # the entry as it is named in the folder, suffix included
  files: tuple[str, ...]  # paths relative to the folder
  total_bytes: int

  @property
  def is_dir(self) -> bool:
    return self.name.endswith(KLSO_SUFFIX)


@dataclass(frozen=True)
class MirrorResult:
  name: str
  sha: str
  previous_sha: str | None
  bundles: tuple[str, ...]
  files: int
  total_bytes: int

  @property
  def unchanged(self) -> bool:
    return self.sha == self.previous_sha


def group_bundles(paths: Mapping[str, int]) -> tuple[RemoteBundle, ...]:
  """Pick the bundles out of a flat listing; anything else is skipped, not refused.

  Raises ValueError if a bundle's name is not a usable app id.
  """
  dirs: dict[str, list[str]] = {}
  singles: dict[str, str] = {}

  for path in paths:
    head, _, _ = path.partition("/")
    if head != path:
      if head.endswith(KLSO_SUFFIX):
        dirs.setdefault(head, []).append(path)
      continue
    for suffix in _SUFFIXES:
      if path.endswith(suffix) and path != suffix:
        singles[path] = suffix
        break

  found: list[RemoteBundle] = []
  for name, files in sorted(dirs.items()):
    if f"{name}/manifest.toml" not in files:
      continue
    found.append(_bundle(name, KLSO_SUFFIX, tuple(sorted(files)), paths))
  for name, suffix in sorted(singles.items()):
    found.append(_bundle(name, suffix, (name,), paths))

  return tuple(sorted(found, key=lambda h: h.app_id))


def _bundle(
  name: str, suffix: str, files: tuple[str, ...], sizes: Mapping[str, int]
) -> RemoteBundle:
  app_id = name.removesuffix(suffix)
  try:
    AppID(app_id)
  except ValueError as e:
    raise ValueError(f"{name} does not name a valid app id: {e}") from e
  return RemoteBundle(
    app_id=app_id,
    name=name,
    files=files,
    total_bytes=sum(sizes[path] for path in files),
  )


def mirror(repo: Repo, ctx) -> MirrorResult:
  """Replace `repos/<name>` with whatever the remote holds now.

  Always a full replacement, never a merge. Raises ValueError for a local repo.
  """
  from kelso.lib import github

  if repo.remote is None:
    raise ValueError(
      f"Repo {repo.name!r} is a local directory; there is nothing to update"
    )

  state = ctx.kelso_db.get_repo_state(repo.name)
  previous = state["sha"] if state else None

  sha = github.resolve_ref(repo.remote)
  entries = github.list_tree(repo.remote, sha)
  sizes = {entry.path: entry.size for entry in entries}
  executable = {entry.path for entry in entries if entry.executable}
  bundles = group_bundles(sizes)
  _check_size(repo, bundles)

  scratch = repo.path.parent / f".update-{repo.name}"
  shutil.rmtree(scratch, ignore_errors=True)
  scratch.mkdir(parents=True)
  files = 0
  total = 0
  try:
    for bundle in bundles:
      for path in bundle.files:
        dest = scratch / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        total += github.download(
          github.raw_url(repo.remote, sha, *path.split("/")), dest
        )
        # Bundles ship scripts that run in-container; losing +x fails only there.
        dest.chmod(0o755 if path in executable else 0o644)
        files += 1
    _swap(scratch, repo.path)
  finally:
    shutil.rmtree(scratch, ignore_errors=True)

  ctx.kelso_db.set_repo_state(repo.name, sha=sha, at=now_ts())
  return MirrorResult(
    name=repo.name,
    sha=sha,
    previous_sha=previous,
    bundles=tuple(bundle.app_id for bundle in bundles),
    files=files,
    total_bytes=total,
  )


def _check_size(repo: Repo, bundles: tuple[RemoteBundle, ...]) -> None:
  files = sum(len(bundle.files) for bundle in bundles)
  total = sum(bundle.total_bytes for bundle in bundles)
  if len(bundles) > MAX_BUNDLES:
    raise ValueError(
      f"{repo.describe()} holds {len(bundles)} bundles, over the {MAX_BUNDLES} limit."
    )
  if files > MAX_REPO_FILES:
    raise ValueError(
      f"{repo.describe()} holds {files} files, over the {MAX_REPO_FILES} limit."
    )
  if total > MAX_REPO_BYTES:
    raise ValueError(
      f"{repo.describe()} is {fmt_size(total)}, over the "
      f"{fmt_size(MAX_REPO_BYTES)} limit for one repo."
    )


def _swap(incoming: Path, dest: Path) -> None:
  """Rename `incoming` onto `dest`, restoring the old copy if that fails."""
  if dest.is_symlink():
    raise ValueError(f"{dest} is a symlink; kelso will not mirror over it")
  outgoing = dest.parent / f".outgoing-{dest.name}"
  shutil.rmtree(outgoing, ignore_errors=True)
  dest.parent.mkdir(parents=True, exist_ok=True)
  try:
    if dest.exists():
      os.replace(dest, outgoing)
    os.replace(incoming, dest)
  except OSError as e:
    if not dest.exists() and outgoing.exists():
      os.replace(outgoing, dest)
    raise ValueError(f"Could not update {dest}: {e}") from e
  finally:
    shutil.rmtree(outgoing, ignore_errors=True)


# --- the repo verbs --------------------------------------------------------


@dataclass(frozen=True)
class AddResult:
  repo: Repo
  mirrored: MirrorResult | None


@dataclass(frozen=True)
class RemoveResult:
  name: str
  bound: tuple[str, ...]


def add(ctx, location: str, *, name: str = "") -> AddResult:
  """Add a `github://` url or a local directory, mirroring a url immediately.

  A local repo needs an explicit `name`; a url takes the repository's own.
  """
  from kelso.lib.config import load_config_file
  from kelso.lib.config_edit import add_repo
  from kelso.lib.kelso import KelsoCtx

  remote = location.startswith(GITHUB_SCHEME)
  name = name or (name_from_url(location) if remote else "")
  if not name:
    raise ValueError(f"Repo {location} needs a name of its own; pass --name")

  with ctx.locked(f"repo add {name}"):
    add_repo(ctx, name, url=location) if remote else add_repo(ctx, name, path=location)
    # `ctx.config` predates the entry just written.
    fresh = KelsoCtx(load_config_file(ctx.config.config_path))
    repo = fresh.config.repos[name]
    return AddResult(repo, mirror(repo, fresh) if remote else None)


def update(ctx, name: str = "") -> tuple[MirrorResult, ...]:
  """Mirror one repo, or every mirrored one. Raises if `name` is local."""
  wanted = [get(ctx, name)] if name else list(ctx.config.repos.values())
  mirrored = [repo for repo in wanted if repo.mirrored]
  if name and not mirrored:
    raise ValueError(f"Repo {name!r} is a local directory; there is nothing to update")
  with ctx.locked("repo update"):
    return tuple(mirror(repo, ctx) for repo in mirrored)


def remove(ctx, name: str) -> RemoveResult:
  """Drop a repo, and the mirrored copy if it had one."""
  import shutil

  from kelso.lib.config_edit import remove_repo

  repo = get(ctx, name)
  if repo.name == MAIN_REPO:
    raise ValueError(f"{MAIN_REPO} is built in and cannot be removed")

  bound = bound_apps(ctx, repo.name)
  with ctx.locked(f"repo remove {name}"):
    remove_repo(ctx, repo.name)
    if repo.mirrored:
      shutil.rmtree(repo.path, ignore_errors=True)
      ctx.kelso_db.del_repo_state(repo.name)
  return RemoveResult(repo.name, bound)


def get(ctx, name: str) -> Repo:
  repo = ctx.config.repos.get(name)
  if repo is None:
    known = ", ".join(sorted(ctx.config.repos))
    raise ValueError(f"No repo {name!r}; configured repos: {known}.")
  return repo


def bound_apps(ctx, name: str) -> tuple[str, ...]:
  """Every installed app recorded as coming from this repo."""
  from kelso.lib.lifecycle import bound_to

  return tuple(
    sorted(
      app_id
      for app_id in ctx.config.app_config_ids()
      if bound_to(app_id, ctx) == f"repo {name}"
    )
  )


def contested_lines(ctx) -> list[str]:
  """One line per app id that more than one repo carries."""
  return [
    f"{app_id} is in {len(repos)} repos ({', '.join(sorted(repos))}); "
    f"install it as {app_id}@<repo>."
    for app_id, repos in sorted(ctx.contested_app_ids().items())
  ]
