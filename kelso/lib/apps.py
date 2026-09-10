from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from kelso.lib.util import validate_identifier

if TYPE_CHECKING:
  from kelso.lib.kelso import KelsoCtx

logger = logging.getLogger("kelso.apps")


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


def record_app_action(action: str, app_id: AppID, ctx: KelsoCtx) -> None:
  """Record informational last-action metadata."""
  ctx.activity_log.write(f"apps/{app_id}/status", action)


def read_last_app_action(app_id: AppID, ctx: KelsoCtx) -> str | None:
  """Read informational last-action metadata."""
  entry = ctx.activity_log.read(f"apps/{app_id}/status")
  return entry.value if entry else None


def read_app_starts(ctx: KelsoCtx) -> dict[str, str]:
  """When kelso last started each app, in one pass over the activity log.

  The activity log is compacted, so an app started long enough ago may not
  appear; callers read that as "unknown", never as "never started".
  """
  starts: dict[str, str] = {}
  for key, entry in ctx.activity_log.history(prefix="apps/", suffix="/status"):
    if entry.value != "started":
      continue
    app_id = key.removeprefix("apps/").removesuffix("/status")
    if app_id:
      starts[app_id] = entry.ts
  return starts


def read_app_actions(ctx: KelsoCtx) -> dict[str, tuple[datetime, str]]:
  """Last recorded action for every app, in one pass over the activity log."""
  actions: dict[str, tuple[datetime, str]] = {}
  for key, entry in ctx.activity_log.scan(prefix="apps/", suffix="/status").items():
    app_id = key.removeprefix("apps/").removesuffix("/status")
    if app_id:
      actions[app_id] = (entry.datetime, entry.value)
  return actions
