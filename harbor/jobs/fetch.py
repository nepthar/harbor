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
  description = "Fetch a happ from GitHub, or update one already fetched"
  required_args = ("target",)
  optional_args = ("yes",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    """`harbor fetch <target>`, with the prompt replaced by an explicit `yes`."""
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
        logger.info(
          "App %s is now available for install, at %s", result.app_id, result.path
        )
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
        lines.append(
          f"Reinstall and restart to pick it up: harbor install {self.app_id}"
        )
      logger.info("\n".join(lines))
