from __future__ import annotations

from logging import getLogger
from pathlib import Path

from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx

logger = getLogger("harbor.lifecycle")


def container_recovery_message(app_id: AppID, ctx: HarborCtx) -> str:
  containers = ctx.run_state(app_id).containers
  ids = ", ".join(container.container_id or container.name for container in containers)
  return (
    f"App {app_id} has Harbor-labeled containers but no usable compose.yml: {ids}. "
    "Refusing to remove state; recover or remove these containers manually."
  )


def managed_volume_dirs(app_id: AppID, ctx: HarborCtx) -> list[Path]:
  return [root / app_id for root in ctx.config.volume_roots.values()]
