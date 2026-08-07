from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from harbor.lib.logtab import LogTab
from harbor.lib.util import validate_identifier

if TYPE_CHECKING:
  from harbor.lib.config import Config

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


def record_app_action(action: str, app_id: AppID, config: Config) -> None:
  """Record informational last-action metadata."""
  key = f"apps/{app_id}/status"
  LogTab(config.activity_log).write(key, action)


def read_last_app_action(app_id: AppID, config: Config) -> str | None:
  """Read informational last-action metadata."""
  key = f"apps/{app_id}/status"
  entry = LogTab(config.activity_log).read(key)
  return entry.value if entry else None


def read_app_actions(config: Config) -> dict[str, tuple[datetime, str]]:
  """Last recorded action for every app, in one pass over the activity log.

  All apps share one log, so callers reporting on many of them should read it
  once rather than per app. Keys are bare app ids.
  """
  actions: dict[str, tuple[datetime, str]] = {}
  for key, entry in (
    LogTab(config.activity_log).scan(prefix="apps/", suffix="/status").items()
  ):
    app_id = key.removeprefix("apps/").removesuffix("/status")
    if app_id:
      actions[app_id] = (entry.datetime(), entry.value)
  return actions
