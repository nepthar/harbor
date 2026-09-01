from harbor.jobs.job import Job, app_target, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stage, start, stop


class RestartJob(Job):
  name = "restart"
  description = "Stop a happ if running, re-stage it, and start it again if it was"
  required_args = ("app",)
  optional_args = ("force",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    self.target = app_target(ctx, kwargs["app"], force=self._bool_arg(kwargs, "force"))
    self.app = str(self.target.app_id)
    self.app_id = self.target.app_id

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"restart {app}", app):
      try:
        running = ctx.run_state(app).running_count
      except ValueError:
        running = 0
      if running:
        stop(app, ctx)
      bundle = self.target.bundle or ctx.bundle_path(app)
      result = stage(app, bundle, ctx, bound=self.target.bound_to)
      if running:
        start(app, ctx.config.app_run_path(app), ctx)
    lines = [f"Restarted {app}" if running else f"Restaged {app}"]
    lines += [
      f"  volume {name} is no longer declared in the manifest; its link is gone "
      f"but its data was left in place"
      for name in result.dropped_volumes
    ]
    logger.info("\n".join(lines))
