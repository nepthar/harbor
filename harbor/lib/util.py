from __future__ import annotations

import os
import re
import string
from pathlib import Path
from typing import Annotated, Protocol

from pydantic import AfterValidator

_IDENTIFIER_RE = re.compile(r"[a-zA-Z0-9_-]+\Z")
_IDENTIFIER_MAX_LEN = 64

# GitHub usernames start and end alphanumeric; `._-` allowed in between.
# Repo names may also start or end with `._-` (`_vibes_` is a real repo).
_GITHUB_USER_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?\Z")
_GITHUB_REPO_RE = re.compile(r"[a-zA-Z0-9._-]+\Z")
_GITHUB_SEGMENT_MAX_LEN = 100


def refuse_root(who: str) -> None:
  """Refuse to keep going when the effective uid is 0.

  Harbor creates the harbor root, run dirs and config files as whoever runs it,
  so running once as root leaves files the real owner cannot rewrite. Nothing
  here needs the privilege: root-owned volume files are handled by a throwaway
  container (see lifecycle/rootfs.py), not by this process.
  """
  if os.geteuid() == 0:
    raise RuntimeError(
      f"{who} refuses to run as root: it would leave root-owned files in the "
      f"harbor root. Re-run it without `sudo`, as the user that owns the "
      f"harbor root."
    )


def validate_identifier(value: str) -> str:
  """Validate a short, unscoped name and return it unchanged."""
  if (
    not value
    or len(value) > _IDENTIFIER_MAX_LEN
    or _IDENTIFIER_RE.fullmatch(value) is None
  ):
    raise ValueError(f"Invalid identifier: {value!r}")
  return value


def validate_github_segment(value: str, kind: str) -> str:
  """Validate a GitHub user or repo name and return it unchanged."""
  pattern = _GITHUB_REPO_RE if kind == "repo" else _GITHUB_USER_RE
  if (
    not value
    or len(value) > _GITHUB_SEGMENT_MAX_LEN
    or (kind == "repo" and value in (".", ".."))
    or pattern.fullmatch(value) is None
  ):
    raise ValueError(f"Invalid GitHub {kind}: {value!r}")
  return value


Identifier = Annotated[str, AfterValidator(validate_identifier)]

# Flat substitution key prefixes. Keys look namespaced (`routes.main`,
# `happ.domain`) but the keyspace itself is flat — see EnvTemplate.
ROUTE_KEY_PREFIX = "routes."
HAPP_KEY_PREFIX = "happ."
HAPP_KEYS = frozenset({"domain", "volumes", "cmd", "routes"})

# What a browser hits. `route.scheme` is the backend dial scheme (how a reverse
# proxy talks to the app) and must not leak into `${routes.*}` URLs.
PUBLIC_ROUTE_SCHEME = "https"


class EnvTemplate(string.Template):
  """`[run.<unit>.env]` placeholders against a flat substitution keyspace.

  Keys may contain a single dot so that `routes.main` and `happ.domain` are
  ordinary map keys, not nested namespaces. The default `Template` pattern
  stops at the first dot, which would make those placeholders invalid and
  leave them unsubstituted — hence the custom `idpattern`.

  At compose time the map holds: declared [config] names (rewritten to
  `${__HARBOR_CONFIG__…}` so secrets stay out of compose.yml), every
  `routes.<name>` URL, and the fixed `happ.*` keys.
  """

  idpattern = r"(?a:[_a-z][_a-z0-9-]*(?:\.[_a-z0-9-]+)?)"


def fmt_size(n: float) -> str:
  for unit in ("B", "KB", "MB", "GB", "TB"):
    if n < 1024:
      return f"{n:.1f} {unit}"
    n /= 1024
  return f"{n:.1f} PB"


def path_size(path: Path) -> int:
  """Total bytes under `path`. Walks every file, so callers building a list
  should think twice before calling it per row."""
  if not path.exists():
    return 0
  if path.is_file():
    return path.stat().st_size
  return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class Conn(Protocol):
  def out(self, data: str): ...

  def err(self, data: str): ...

  def read(self, prompt: str = "") -> str: ...
