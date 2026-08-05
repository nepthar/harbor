from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from harbor.lib.logtab import LogTab
from harbor.lib.util import validate_identifier

if TYPE_CHECKING:
  from harbor.lib.config import Config
  from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.apps")


class AppID(str):
  def __new__(cls, value: str | AppID) -> AppID:
    if isinstance(value, AppID):
      return value
    for part in value.split("."):
      validate_identifier(part)
    return super().__new__(cls, value)

  @property
  def parts(self) -> tuple[str, ...]:
    return tuple(self.split("."))

  @property
  def stem(self) -> str:
    """com.company.my-app -> my-app"""
    return self.parts[-1]


def app_id_from_path(path: Path) -> AppID:
  """The app id a bundle path carries: `<id>.happ` dir or `<id>.happ.md` file."""
  if path.name.endswith(".happ.md"):
    if not path.is_file():
      raise ValueError(f"{path} is not a file")
    return AppID(path.name.removesuffix(".happ.md"))
  if not path.is_dir():
    raise ValueError(f"{path} is not a directory")
  if path.suffix != ".happ":
    raise ValueError(f"{path} is not a happ bundle: directory name must end in .happ")
  if not (path / "manifest.toml").is_file():
    raise ValueError(f"{path} is not a happ bundle: missing manifest.toml")
  return AppID(path.stem)


def is_pathlike(raw: str) -> bool:
  """Decide whether an APP argument names a filesystem path vs an app id.

  A valid happ path ends in `.happ` (or `.happ.md`), so a bare name without
  either suffix can never be a path.
  """
  return (
    os.sep in raw or raw.startswith(("~", ".")) or raw.endswith((".happ", ".happ.md"))
  )


def resolve_app_id(ctx: HarborCtx, raw: str) -> AppID:
  """Resolve an APP argument -- an app id, or a path to a .happ -- to an id."""
  if is_pathlike(raw):
    return app_id_from_path(Path(raw).expanduser().resolve())
  return ctx.resolve_app(raw)


def record_app_action(action: str, app_id: AppID, config: Config) -> None:
  """Record informational last-action metadata."""
  key = f"{app_id}/status"
  LogTab(config.activity_log).write(key, action)


def read_last_app_action(app_id: AppID, config: Config) -> str | None:
  """Read informational last-action metadata."""
  key = f"{app_id}/status"
  return LogTab(config.activity_log).read(key)


def read_app_actions(config: Config) -> dict[str, str]:
  """Last recorded action for every app, in one pass over the activity log.

  All apps share one log, so callers reporting on many of them should read it
  once rather than per app.
  """
  actions: dict[str, str] = {}
  for key, action in LogTab(config.activity_log).load().items():
    app_id, _, field = key.rpartition("/")
    if field == "status" and app_id:
      actions[app_id] = action
  return actions
