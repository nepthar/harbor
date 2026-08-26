from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import start
from harbor.lib.receipt import published_urls


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"start {app}", app):
    # Mirrors `harbor start`: prefer the run copy once staged, so an app whose
    # catalog entry has since been deleted still starts.
    bundle = (
      ctx.config.app_run_path(app) if ctx.is_staged(app) else ctx.bundle_path(app)
    )
    result = start(app, bundle, ctx)
    lines = [f"Started {app}"]
    lines += [f"  {url}" for url in published_urls(result.stack, result.run_data, ctx)]
    return "\n".join(lines)
