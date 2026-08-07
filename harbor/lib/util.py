from __future__ import annotations

import re
import string
from typing import Annotated, Protocol

from pydantic import AfterValidator

_IDENTIFIER_RE = re.compile(r"[a-zA-Z0-9_-]+\Z")
_IDENTIFIER_MAX_LEN = 64

# GitHub owner and repo names: start and end alphanumeric, `._-` in between.
_GITHUB_SEGMENT_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?\Z")
_GITHUB_SEGMENT_MAX_LEN = 100


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
  if (
    not value
    or len(value) > _GITHUB_SEGMENT_MAX_LEN
    or _GITHUB_SEGMENT_RE.fullmatch(value) is None
  ):
    raise ValueError(f"Invalid GitHub {kind}: {value!r}")
  return value


Identifier = Annotated[str, AfterValidator(validate_identifier)]

# The namespace prefix in `${routes.<name>}`.
ROUTE_NAMESPACE = "routes"


class EnvTemplate(string.Template):
  """What `[run.<unit>.env]` values may reference.

  `${name}` is a [config] value; `${routes.<name>}` is a route's public URL.
  Config names are plain identifiers, so the dot is what tells the two apart --
  and it is why the default `Template` pattern (which stops at the dot, making
  `${routes.main}` an invalid placeholder that substitution silently ignores)
  is not enough.
  """

  idpattern = r"(?a:[_a-z][_a-z0-9-]*(?:\.[_a-z0-9-]+)?)"


def fmt_size(n: float) -> str:
  for unit in ("B", "KB", "MB", "GB", "TB"):
    if n < 1024:
      return f"{n:.1f} {unit}"
    n /= 1024
  return f"{n:.1f} PB"


class Conn(Protocol):
  def out(self, data: str): ...

  def err(self, data: str): ...

  def read(self, prompt: str = "") -> str: ...
