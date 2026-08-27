from harbor.jobs.job import Job, logger
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


class FetchJob(Job):
  name = "fetch"
  description = "Install a happ from GitHub, or update one already fetched"
  required_args = ("target",)
  optional_args = ("yes",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    """`harbor fetch <target>`, with the prompt replaced by an explicit `yes`.

    The CLI asks before a first install, because harbor cannot vouch for a
    happ's author and the receipt is the only check there is. A job cannot be
    asked anything, so the caller has to have decided already -- a client
    that wants the operator to see what they are approving shows them the
    preview first. An update carries no prompt in the CLI either, so it
    needs no `yes` here.
    """
    self.target = kwargs["target"]
    spec, pin = split_pin(self.target)
    self.spec = spec
    self.pin = pin
    self.yes = self._bool_arg(kwargs, "yes")

    if spec.startswith(GITHUB_PREFIX):
      if not self.yes:
        raise ValueError(
          f"Fetching {spec} installs code harbor cannot vouch for. Read its "
          f"manifest first, then resubmit with yes=1 to confirm."
        )
      self.mode = "install"
      return

    if is_pathlike(spec):
      raise ValueError(
        f"Don't know how to fetch {self.target!r}; expected a github: target "
        f"or an installed app id.\n  {USAGE}"
      )
    if "@" in self.target and pin is None:
      raise ValueError(
        f"Pin must be a full 40-character commit sha, not "
        f"{self.target.rsplit('@', 1)[-1]!r}"
      )

    self.mode = "update"
    app = ctx.resolve_app(spec)
    self.app = str(app)
    self.app_id = app

  def run(self, ctx: HarborCtx) -> None:
    with ctx.harbor_lock(f"fetch {self.target}"):
      if self.mode == "install":
        result = install_target(self.spec, self.pin, ctx)
        logger.info("Installed %s at %s", result.app_id, result.path)
        return

      result = update_app(self.app_id, self.pin, ctx)
      if not isinstance(result, FetchResult):
        logger.info("%s", result)
        return
      lines = [
        f"Updated {self.app_id}",
        f" - {result.previous}",
        f" + {result.current}",
      ]
      if result.staged:
        lines.append(f"Re-stage and restart to pick it up: harbor stage {self.app_id}")
      logger.info("\n".join(lines))
