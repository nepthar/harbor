from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import resolve_snapshot_app, restore, restore_plan


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  """Replace an app's run state with a snapshot's, what `harbor restore -y` does.

  The app is resolved against snapshots/, so a removed app can still come back.
  A pre-restore snapshot is taken when there is live state to overwrite.
  """
  app = resolve_snapshot_app(ctx, args["app"])
  plan = restore_plan(app, args["snapshot"], ctx)
  with ctx.locked(f"restore {app}", app):
    restore(plan, ctx)
  return f"Restored {plan.app_id} from {plan.snapshot_path}"
