from harbor.jobs.job import Job, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stop


class StopJob(Job):
  name = "stop"
  description = "Stop a running happ"
  required_args = ("app",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"stop {app}", app):
      stop(app, ctx)
    logger.info("Stopped %s", app)
