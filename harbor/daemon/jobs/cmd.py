import shlex

from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import run_command


def run(ctx: HarborCtx, args: dict[str, str]) -> str:
  """Run a manifest `[commands]` entry, exactly what `harbor cmd` does.

  `command` names an entry the happ's manifest already declares -- an id of a
  thing that exists, like every other verb's argument. Extra tokens in `args`
  are forwarded the same way the CLI's remainder is: they cannot pick a
  different binary. Any output the command produced is captured into the job
  (and its activity file) by the runner; a non-zero exit fails the job so it
  reads as an error rather than a silent success.
  """
  extra = []
  raw = args.get("args", "")
  if raw.strip():
    try:
      extra = shlex.split(raw)
    except ValueError as e:
      raise ValueError(f"Could not parse arguments: {e}") from e

  app = ctx.resolve_app(args["app"])
  with ctx.locked(f"cmd {app}", app):
    code = run_command(app, args["command"], extra, ctx)
    if code != 0:
      raise ValueError(
        f"Command {args['command']!r} for {app} exited with status {code}"
      )
    return f"Ran command {args['command']!r} for {app}"
