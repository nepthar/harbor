from harbor.jobs.job import Job, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import start
from harbor.lib.receipt import published_urls


class StartJob(Job):
  name = "start"
  description = "Start a happ, staging it first if needed"
  required_args = ("app",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"start {app}", app):
      # Prefer the run copy once staged, so an app whose catalog entry has
      # since been deleted still starts.
      bundle = (
        ctx.config.app_run_path(app) if ctx.is_staged(app) else ctx.bundle_path(app)
      )
      result = start(app, bundle, ctx)
      lines = [f"Started {app}"]
      lines += [
        f"  {url}" for url in published_urls(result.stack, result.run_data, ctx)
      ]
      logger.info("\n".join(lines))
