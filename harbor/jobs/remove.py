from harbor.jobs.job import Job, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import PURGE, RESET, UNINSTALL, removal_plan, rm


class UninstallJob(Job):
  name = "uninstall"
  description = "Uninstall a happ, keeping its data and config unless purged"
  required_args = ("app",)
  optional_args = ("purge",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app
    self.mode = PURGE if self._bool_arg(kwargs, "purge") else UNINSTALL

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"{self.mode} {app}", app):
      # Planning resolves what is about to go before anything is deleted.
      plan = removal_plan(app, ctx, mode=self.mode)
      rm(plan, ctx)
    if self.mode == PURGE:
      logger.info("Removed %s, including its data and configuration", app)
    else:
      logger.info("Uninstalled %s. Configuration and volume data were kept", app)


class ResetJob(Job):
  name = "reset"
  description = "Delete a happ's data and install it again from the bundle"
  required_args = ("app",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"reset {app}", app):
      plan = removal_plan(app, ctx, mode=RESET)
      rm(plan, ctx)
    logger.info("Reset %s. Its configuration and address are unchanged", app)
