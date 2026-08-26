from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stop


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"stop {app}", app):
    stop(app, ctx)
  return f"Stopped {app}"
