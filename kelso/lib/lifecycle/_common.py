from __future__ import annotations

from logging import getLogger
from pathlib import Path

from kelso.lib.apps import AppID
from kelso.lib.kelso import KelsoCtx

logger = getLogger("kelso.lifecycle")


def container_recovery_message(app_id: AppID, ctx: KelsoCtx) -> str:
  containers = ctx.run_state(app_id).containers
  ids = ", ".join(container.container_id or container.name for container in containers)
  return (
    f"App {app_id} has Kelso-labeled containers but no usable compose.yml: {ids}. "
    "Refusing to remove state; recover or remove these containers manually."
  )


def managed_volume_dirs(app_id: AppID, ctx: KelsoCtx) -> list[Path]:
  return [root / app_id for root in ctx.config.volume_roots.values()]
