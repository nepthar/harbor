from harbor.jobs.job import Job, app_target, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import reload_app


class ReloadJob(Job):
  name = "reload"
  description = "Stop a happ if running, re-install it, and start it again if it was"
  required_args = ("app",)
  optional_args = ("force",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    self.target = app_target(ctx, kwargs["app"], force=self._bool_arg(kwargs, "force"))
    self.app = str(self.target.app_id)
    self.app_id = self.target.app_id

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"reload {app}", app):
      result = reload_app(
        app,
        self.target.bundle or ctx.bundle_path(app),
        ctx,
        bound=self.target.bound_to,
      )
    lines = [f"Reloaded {app}" if result.was_running else f"Re-installed {app}"]
    lines += [
      f"  volume {name} is no longer declared in the manifest; its link is gone "
      f"but its data was left in place"
      for name in result.stage.dropped_volumes
    ]
    logger.info("\n".join(lines))
