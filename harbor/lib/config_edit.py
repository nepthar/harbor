"""Editing config.toml in place.

config.toml is a file the operator writes and reads: the template ships with
more comment than content, and their own notes accumulate beside it. So edits
go through tomlkit, which round-trips comments, ordering and whitespace. A
tool that reformats the file every time it touches it is a tool the operator
stops letting near it.

Two rules hold for every edit here:

**The lock is held.** config.toml is harbor-wide state, and two writers would
lose one of the edits.

**Nothing is committed until it loads.** The new text is written beside the
original, parsed and validated by the same `load_config_file` every command
uses, and only then moved into place. A rejected edit costs an error message;
it never costs a harbor that no longer starts.
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
def edit_config(ctx: HarborCtx, by: str) -> Iterator[TOMLDocument]:
  """Yield config.toml as a tomlkit document; write it back if it validates.

  `by` names the edit in the lock record, the same way a command does.
  """
  path = ctx.config.config_path
  with ctx.lock(by):
    document = tomlkit.parse(path.read_text())
    yield document
    _commit(path, tomlkit.dumps(document))


def _commit(path: Path, text: str) -> None:
  """Replace `path` with `text`, but only once it loads as a harbor config.

  The temp file is a sibling so the rename stays on one filesystem, which is
  what makes it atomic -- no reader ever sees a half-written config.
  """
  staging = path.with_name(f".{path.name}.incoming")
  staging.write_text(text)
  try:
    load_config_file(staging)
  except (ValueError, RuntimeError) as e:
    staging.unlink(missing_ok=True)
    raise ValueError(f"Refusing to write {path}: the result is not valid.\n{e}") from e
  os.replace(staging, path)


def _host_volumes(document: TOMLDocument):
  """The `[host_volume]` table, created on first use.

  `super_table` keeps it as bare `[host_volume.<tag>]` headers rather than a
  `[host_volume]` parent with children under it -- which is how the config
  template writes them, and how an operator expects to find them.
  """
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
  """Refuse a path that is not there.

  A typo caught here is a typo the operator fixes now; accepted, it surfaces
  later as an app that will not start. Resolution goes through the loader's own
  expansion, so what is checked is exactly what harbor will bind -- including
  `${harbor_root}` and a path taken relative to config.toml.
  """
  resolved = _expand_path(raw, ctx.config.config_path.parent, ctx.config.harbor_root)
  if not resolved.exists():
    raise ValueError(
      f"No such directory: {resolved}\n"
      f"Create it first, or point the host volume somewhere that exists."
    )
  if not resolved.is_dir():
    raise ValueError(f"Host volume path is not a directory: {resolved}")
  if require_mount and not resolved.is_mount():
    # Not an error: a share being down is exactly the condition require_mount
    # exists to catch at start time, and the operator has to be able to
    # configure harbor while it is down.
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
  with edit_config(ctx, f"host-volume add {tag}") as document:
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
  with edit_config(ctx, f"host-volume set {tag}") as document:
    _host_volumes(document)[tag] = _entry(
      path, readonly=readonly, require_mount=require_mount
    )


def remove_host_volume(ctx: HarborCtx, tag: str) -> None:
  """Drop an entry. Apps bound to it keep the bind on file and start failing
  with a message naming the tag, which is `bind`'s existing story for a tag
  that is not configured."""
  if tag not in ctx.config.host_volumes:
    known = ", ".join(sorted(ctx.config.host_volumes)) or "(none)"
    raise ValueError(f"No host volume {tag!r}; known tags: {known}")
  with edit_config(ctx, f"host-volume rm {tag}") as document:
    del _host_volumes(document)[tag]
