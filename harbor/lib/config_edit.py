"""Editing config.toml in place.

Edits go through tomlkit, which round-trips the operator's own comments,
ordering and whitespace. Two rules hold for every edit here: the harbor lock is
held, and nothing is committed until the new text parses through
`load_config_file`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import tomlkit
from tomlkit import TOMLDocument

from harbor.lib.config import _expand_path, load_config_file
from harbor.lib.util import validate_identifier

logger = logging.getLogger("harbor.config_edit")

if TYPE_CHECKING:
  from harbor.lib.harbor import HarborCtx


@contextmanager
def edit_config(ctx: HarborCtx) -> Iterator[TOMLDocument]:
  """Yield config.toml as a tomlkit document; write it back if it validates."""
  path = ctx.config.config_path
  document = tomlkit.parse(path.read_text())
  yield document
  _commit(path, tomlkit.dumps(document))


def _commit(path: Path, text: str) -> None:
  """Replace `path` with `text`, but only once it loads as a harbor config."""
  staging = path.with_name(f".{path.name}.incoming")
  staging.write_text(text)
  try:
    load_config_file(staging)
  except (ValueError, RuntimeError) as e:
    staging.unlink(missing_ok=True)
    raise ValueError(f"Refusing to write {path}: the result is not valid.\n{e}") from e
  os.replace(staging, path)


def _host_volumes(document: TOMLDocument):
  """The `[host_volume]` table, created on first use."""
  if "host_volume" not in document:
    document["host_volume"] = tomlkit.table(is_super_table=True)
  return document["host_volume"]


def _entry(path: str, *, readonly: bool, require_mount: bool):
  table = tomlkit.table()
  table["path"] = path
  if readonly:
    table["readonly"] = True
  if require_mount:
    table["require_mount"] = True
  return table


def _check_path(ctx: HarborCtx, raw: str, *, require_mount: bool) -> None:
  """Refuse a path that is not there."""
  resolved = _expand_path(raw, ctx.config.config_path.parent, ctx.config.harbor_root)
  if not resolved.exists():
    raise ValueError(
      f"No such directory: {resolved}\n"
      f"Create it first, or point the host volume somewhere that exists."
    )
  if not resolved.is_dir():
    raise ValueError(f"Host volume path is not a directory: {resolved}")
  if require_mount and not resolved.is_mount():
    # Not an error: a share being down is what require_mount catches at start
    # time, and harbor has to stay configurable while it is down.
    logger.warning(
      "%s is not a mount point right now; require_mount will refuse to start "
      "an app bound to it until the share is mounted",
      resolved,
    )


def add_host_volume(
  ctx: HarborCtx,
  tag: str,
  path: str,
  *,
  readonly: bool = False,
  require_mount: bool = False,
) -> None:
  validate_identifier(tag)
  if tag in ctx.config.host_volumes:
    raise ValueError(
      f"Host volume {tag!r} already exists ({ctx.config.host_volumes[tag].path}). "
      f"Change it with `harbor config-sys host-volume --set {tag}=<path>`."
    )
  _check_path(ctx, path, require_mount=require_mount)
  with edit_config(ctx) as document:
    _host_volumes(document)[tag] = _entry(
      path, readonly=readonly, require_mount=require_mount
    )


def set_host_volume(
  ctx: HarborCtx,
  tag: str,
  path: str,
  *,
  readonly: bool = False,
  require_mount: bool = False,
) -> None:
  """Replace an existing entry. Flags are the new whole truth, not a patch:
  omitting --readonly clears it, the same as it would on a fresh add."""
  if tag not in ctx.config.host_volumes:
    known = ", ".join(sorted(ctx.config.host_volumes)) or "(none)"
    raise ValueError(
      f"No host volume {tag!r}; known tags: {known}. "
      f"Add it with `harbor config-sys host-volume --add {tag}=<path>`."
    )
  _check_path(ctx, path, require_mount=require_mount)
  with edit_config(ctx) as document:
    _host_volumes(document)[tag] = _entry(
      path, readonly=readonly, require_mount=require_mount
    )


def remove_host_volume(ctx: HarborCtx, tag: str) -> None:
  """Drop a `[host_volume]` entry."""
  if tag not in ctx.config.host_volumes:
    known = ", ".join(sorted(ctx.config.host_volumes)) or "(none)"
    raise ValueError(f"No host volume {tag!r}; known tags: {known}")
  with edit_config(ctx) as document:
    del _host_volumes(document)[tag]
