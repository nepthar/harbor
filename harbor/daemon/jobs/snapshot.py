from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import snapshot, start, stop


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  """Stop if running, copy, start if we stopped.

  Holds the app lock the whole time so nothing else mutates this app. The
  harbor lock is only around stop and start, so other apps can proceed while
  volumes copy. `snapshot()` is the copy; it assumes the app lock and a
  stopped app.
  """
  app = ctx.resolve_app(args["app"])
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
      path = snapshot(app, ctx, label=args.get("label", ""))
    finally:
      if running:
        with ctx.harbor_lock(by):
          start(app, ctx.config.app_run_path(app), ctx)
  return f"Snapshot of {app} written to {path}"
