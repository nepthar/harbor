from harbor.jobs.job import Job, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stage, start, stop


class RestartJob(Job):
  name = "restart"
  description = "Stop a happ if running, re-stage it, and start it again if it was"
  required_args = ("app",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"restart {app}", app):
      try:
        running = ctx.run_state(app).running_count
      except ValueError:
        running = 0
      if running:
        stop(app, ctx)
      result = stage(app, ctx.bundle_path(app), ctx)
      if running:
        start(app, ctx.config.app_run_path(app), ctx)
    lines = [f"Restarted {app}" if running else f"Restaged {app}"]
    lines += [
      f"  volume {name} is no longer declared in the manifest; its link is gone "
      f"but its data was left in place"
      for name in result.dropped_volumes
    ]
    logger.info("\n".join(lines))
