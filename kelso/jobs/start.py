from kelso.jobs.job import Job, app_target, logger
from kelso.lib.kelso import KelsoCtx
from kelso.lib.lifecycle import start
from kelso.lib.receipt import published_urls


class StartJob(Job):
  name = "start"
  description = "Start a bundle, staging it first if needed"
  required_args = ("app",)
  optional_args = ("force",)

  def init(self, ctx: KelsoCtx, kwargs: dict[str, str]) -> None:
    self.target = app_target(ctx, kwargs["app"], force=self._bool_arg(kwargs, "force"))
    self.app = str(self.target.app_id)
    self.app_id = self.target.app_id

  def run(self, ctx: KelsoCtx) -> None:
    app = self.app_id
    with ctx.locked(f"start {app}", app):
      # Prefer the run copy once staged, so an app whose catalog entry has
      # since been deleted still starts.
      bundle = (
        ctx.config.app_run_path(app)
        if ctx.is_staged(app)
        else (self.target.bundle or ctx.bundle_path(app))
      )
      result = start(app, bundle, ctx, bound=self.target.bound_to)
      lines = [f"Started {app}"]
      lines += [
        f"  {url}" for url in published_urls(result.stack, result.run_data, ctx)
      ]
      logger.info("\n".join(lines))
