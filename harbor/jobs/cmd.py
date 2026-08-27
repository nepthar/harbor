import shlex

from harbor.jobs.job import Job, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle import run_command
from harbor.lib.stack import AppStack


class CmdJob(Job):
  name = "cmd"
  description = "Run a command declared in a happ's manifest"
  required_args = ("app", "command")
  optional_args = ("args",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    """`command` names an entry the happ's manifest already declares.

    Extra tokens in `args` are forwarded the same way the CLI's remainder is:
    they cannot pick a different binary. A caller that starts from an argv
    list must build `args` with `shlex.join`, so the split here recovers the
    tokens exactly.
    """
    app = ctx.resolve_app(kwargs["app"])
    extra = []
    raw = kwargs.get("args", "")
    if raw.strip():
      try:
        extra = shlex.split(raw)
      except ValueError as e:
        raise ValueError(f"Could not parse arguments: {e}") from e

    if not ctx.is_staged(app):
      raise ValueError(f"App {app} is not installed; run `harbor install {app}` first")
    stack = AppStack.from_file(ctx.staged_paths(app).manifest_path, app)
    if kwargs["command"] not in stack.commands:
      available = ", ".join(sorted(stack.commands)) or "(none)"
      raise ValueError(
        f"Unknown command {kwargs['command']!r} for {app}; "
        f"available: {available}. List with `harbor cmd {app}`"
      )

    self.app = str(app)
    self.app_id = app
    self.command = kwargs["command"]
    self.extra = extra

  def run(self, ctx: HarborCtx) -> None:
    app = self.app_id
    with ctx.app_lock(app, f"cmd {app}"):
      code = run_command(app, self.command, self.extra, ctx)
      if code != 0:
        raise ValueError(
          f"Command {self.command!r} for {app} exited with status {code}"
        )
    logger.info("Ran command %r for %s", self.command, app)
