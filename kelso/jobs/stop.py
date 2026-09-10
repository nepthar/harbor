import kelso.lib.lifecycle as lifecycle
from kelso.jobs.job import Job, logger
from kelso.lib.kelso import KelsoCtx


class StopJob(Job):
  name = "stop"
  description = "Stop a running bundle"
  required_args = ("app",)

  def init(self, ctx: KelsoCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: KelsoCtx) -> None:
    app = self.app_id
    with ctx.locked(f"stop {app}", app):
      lifecycle.stop(app, ctx)
    logger.info("Stopped %s", app)
