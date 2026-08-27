from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stage
from harbor.ops.operation import BaseOp, logger


class StageOp(BaseOp):
  name = "stage"
  description = "Install a happ into the run directory"
  required_args = ("app",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.locked(f"stage {app}", app):
      result = stage(app, ctx.bundle_path(app), ctx)
      lines = [f"Staged {app} at {ctx.run_path(app)}"]
      lines += [
        f"  volume {name} is no longer declared in the manifest; its link is gone "
        f"but its data was left in place"
        for name in result.dropped_volumes
      ]
      logger.info("\n".join(lines))
