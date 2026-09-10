from kelso.jobs.job import Job, logger
from kelso.lib.kelso import KelsoCtx
from kelso.lib.lifecycle import (
  resolve_snapshot_app,
  restore,
  restore_plan,
  snapshot_names,
)
from kelso.lib.lifecycle.snapshot import SNAPSHOT_TAR_SUFFIX


class RestoreJob(Job):
  name = "restore"
  description = "Replace an app's run state with a snapshot"
  required_args = ("app", "snapshot")

  def init(self, ctx: KelsoCtx, kwargs: dict[str, str]) -> None:
    """The app is resolved against snapshots/, so a removed app can still come back."""
    app = resolve_snapshot_app(ctx, kwargs["app"])
    name = kwargs["snapshot"].removesuffix(SNAPSHOT_TAR_SUFFIX)
    available = snapshot_names(app, ctx)
    if name not in available:
      detail = "\n".join(f"  {n}" for n in available) if available else "  (none)"
      raise ValueError(f"No snapshot {name} for {app}. Available:\n{detail}")
    self.app = str(app)
    self.app_id = app
    self.snapshot = name

  def run(self, ctx: KelsoCtx) -> None:
    app = self.app_id
    plan = restore_plan(app, self.snapshot, ctx)
    with ctx.locked(f"restore {app}", app):
      restore(plan, ctx)
    logger.info("Restored %s from %s", plan.app_id, plan.snapshot_path)
