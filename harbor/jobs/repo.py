"""Repo verbs the daemon can be asked for.

`repo-add` takes a `github://` url and never a directory: naming one over the
wire is the capability `runner.JOBS` refuses. Local repos are CLI-only.
"""

from harbor.jobs.job import Job, logger
from harbor.lib import repo as repo_lib
from harbor.lib.harbor import HarborCtx
from harbor.lib.repo import GITHUB_SCHEME, USAGE, parse_github_url
from harbor.lib.util import fmt_size


class RepoAddJob(Job):
  name = "repo-add"
  description = "Add a GitHub folder of happs as a repo, and mirror it"
  required_args = ("url",)
  optional_args = ("name",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    url = kwargs["url"].strip()
    if not url.startswith(GITHUB_SCHEME):
      raise ValueError(
        f"repo-add takes a {GITHUB_SCHEME} url, not {url!r}.\n  {USAGE}\n"
        f"A directory on this machine is added with `harbor repo add` instead."
      )
    # Before anything is written, so a malformed url never reaches config.toml.
    parse_github_url(url)
    self.url = url
    self.repo_name = kwargs.get("name", "").strip()

  def run(self, ctx: HarborCtx) -> None:
    result = repo_lib.add(ctx, self.url, name=self.repo_name)
    lines = [f"Added repo {result.repo.name} -> {result.repo.describe()}"]
    if result.mirrored is not None:
      done = result.mirrored
      lines.append(
        f"Mirrored {len(done.happs)} happs at {done.sha[:8]} "
        f"({fmt_size(done.total_bytes)})"
      )
      lines += [f"  {app_id}" for app_id in done.happs]
    lines += _contested(ctx)
    logger.info("\n".join(lines))


class RepoUpdateJob(Job):
  name = "repo-update"
  description = "Bring mirrored repos up to whatever the remote holds now"
  optional_args = ("name",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    self.repo_name = kwargs.get("name", "").strip()
    if self.repo_name:
      repo_lib.get(ctx, self.repo_name)

  def run(self, ctx: HarborCtx) -> None:
    results = repo_lib.update(ctx, self.repo_name)
    if not results:
      logger.info("No mirrored repos to update.")
      return
    lines = []
    for result in results:
      if result.unchanged:
        lines.append(f"{result.name}: already at {result.sha[:8]}")
      else:
        lines.append(
          f"{result.name}: {result.sha[:8]} "
          f"({len(result.happs)} happs, {fmt_size(result.total_bytes)})"
        )
    lines += _contested(ctx)
    logger.info("\n".join(lines))


class RepoRemoveJob(Job):
  name = "repo-remove"
  description = "Drop a repo and the copy it mirrored"
  required_args = ("name",)

  def init(self, ctx: HarborCtx, kwargs: dict[str, str]) -> None:
    self.repo_name = kwargs["name"].strip()
    repo_lib.get(ctx, self.repo_name)

  def run(self, ctx: HarborCtx) -> None:
    result = repo_lib.remove(ctx, self.repo_name)
    lines = [f"Removed repo {result.name}"]
    if result.bound:
      lines.append(
        f"These apps were installed from it: {', '.join(result.bound)}. They keep "
        f"running -- what is staged under run/ is already a copy -- but harbor "
        f"will no longer see updates for them."
      )
    logger.info("\n".join(lines))


def _contested(ctx: HarborCtx) -> list[str]:
  """Ids more than one repo now carries. `ctx.config` predates the change."""
  from harbor.lib.config import load_config_file

  fresh = HarborCtx(load_config_file(ctx.config.config_path))
  return repo_lib.contested_lines(fresh)
