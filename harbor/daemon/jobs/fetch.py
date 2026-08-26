from harbor.lib.fetch import (
  GITHUB_PREFIX,
  USAGE,
  FetchResult,
  install_target,
  split_pin,
  update_app,
)
from harbor.lib.happ import is_pathlike
from harbor.lib.harbor import HarborCtx


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  """`harbor fetch <target>`, with the prompt replaced by an explicit `yes`.

  The CLI asks before a first install, because harbor cannot vouch for a happ's
  author and the receipt is the only check there is. A job cannot be asked
  anything, so the caller has to have decided already -- a client that wants
  the operator to see what they are approving shows them the preview first.
  An update carries no prompt in the CLI either, so it needs no `yes` here.
  """
  with ctx.harbor_lock(f"fetch {args['target']}"):
    spec, pin = split_pin(args["target"])
    if spec.startswith(GITHUB_PREFIX):
      if args.get("yes") not in ("1", "true", "yes"):
        raise ValueError(
          f"Fetching {spec} installs code harbor cannot vouch for. Read its "
          f"manifest first, then resubmit with yes=1 to confirm."
        )
      result = install_target(spec, pin, ctx)
      return f"Installed {result.app_id} at {result.path}"

    if is_pathlike(spec):
      raise ValueError(
        f"Don't know how to fetch {args['target']!r}; expected a github: target "
        f"or an installed app id.\n  {USAGE}"
      )
    if "@" in args["target"] and pin is None:
      raise ValueError(
        f"Pin must be a full 40-character commit sha, not "
        f"{args['target'].rsplit('@', 1)[-1]!r}"
      )

    app = ctx.resolve_app(spec)
    result = update_app(app, pin, ctx)
    if not isinstance(result, FetchResult):
      return result
    lines = [f"Updated {app}", f" - {result.previous}", f" + {result.current}"]
    if result.staged:
      lines.append(f"Re-stage and restart to pick it up: harbor stage {app}")
    return "\n".join(lines)
