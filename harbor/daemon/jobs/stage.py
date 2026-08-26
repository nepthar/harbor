from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import stage


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"stage {app}", app):
    result = stage(app, ctx.bundle_path(app), ctx)
    lines = [f"Staged {app} at {ctx.run_path(app)}"]
    lines += [
      f"  volume {name} is no longer declared in the manifest; its link is gone "
      f"but its data was left in place"
      for name in result.dropped_volumes
    ]
    return "\n".join(lines)
