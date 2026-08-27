from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import snapshot, start, stop
from harbor.ops.operation import BaseOp, logger


class SnapshotOp(BaseOp):
  name = "snapshot"
  description = "Copy an app's volumes and run state to a snapshot archive"
  required_args = ("app",)
  optional_args = ("label",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    app = ctx.resolve_app(kwargs["app"])
    self.app = str(app)
    self.app_id = app
    self.label = kwargs.get("label", "")

  def run(self, ctx: HarborCtx) -> None:
    """Stop if running, copy, start if we stopped.

    Holds the app lock the whole time so nothing else mutates this app. The
    harbor lock is only around stop and start, so other apps can proceed while
    volumes copy. `snapshot()` is the copy; it assumes the app lock and a
    stopped app.
    """
    app = self.app_id
    by = f"snapshot {app}"
    running = 0
    with ctx.app_lock(app, by):
      with ctx.harbor_lock(by):
        try:
          running = ctx.run_state(app).running_count
        except ValueError:
          running = 0
        if running:
          stop(app, ctx)
      try:
        path = snapshot(app, ctx, label=self.label)
      finally:
        if running:
          with ctx.harbor_lock(by):
            start(app, ctx.config.app_run_path(app), ctx)
    logger.info("Snapshot of %s written to %s", app, path)
