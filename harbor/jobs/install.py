from harbor.jobs.job import Job, app_target, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stage


class InstallJob(Job):
  name = "install"
  description = "Install a happ from the catalog so it can be started"
  required_args = ("app",)
  optional_args = ("force",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    self.target = app_target(ctx, kwargs["app"], force=self._bool_arg(kwargs, "force"))
    self.app = str(self.target.app_id)
    self.app_id = self.target.app_id

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"stage {app}", app):
      bundle = self.target.bundle or ctx.bundle_path(app)
      result = stage(app, bundle, ctx, bound=self.target.bound_to)
      lines = [f"Installed {app} at {ctx.run_path(app)}"]
      lines += [
        f"  volume {name} is no longer declared in the manifest; its link is gone "
        f"but its data was left in place"
        for name in result.dropped_volumes
      ]
      logger.info("\n".join(lines))
