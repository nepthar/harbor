from __future__ import annotations

from pathlib import Path

from harbor.lib.apps import AppID, record_app_action
from harbor.lib.docker import DockerError, docker_run_command
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle._common import container_recovery_message, logger
from harbor.lib.lifecycle.routes import (
  preflight_app_routes,
  register_app_routes,
  unregister_app_routes,
)
from harbor.lib.lifecycle.stage import (
  StageSuccess,
  link_ext_volumes,
  stage,
  unlink_ext_volumes,
)
from harbor.lib.routes import RouteProviderError
from harbor.lib.run_layout import ConfigIssue, load_run_data
from harbor.lib.stack import AppStack


def recovery_lines(app_id: AppID, issues: tuple[ConfigIssue, ...]) -> list[str]:
  """Turn start blockers into what is wrong and how to fix it.

  Naming the problem matters as much as the remedy: several issues share the
  same fix, so listing fixes alone produced repeated lines that never said
  which value or volume was at fault.
  """
  lines = [f"{app_id} cannot start:"]
  for issue in issues:
    lines.append(f"  - {issue.problem}")
    if issue.fix:
      lines.append(f"    {issue.fix}")
  return lines


def start(
  app: AppID,
  bundle: Path,
  ctx: HarborCtx,
  *,
  sets: list[tuple[str, str]] | None = None,
  binds: list[tuple[str, str]] | None = None,
) -> StageSuccess:
  """Stage if needed, then bring the app up and publish its web routes.

  `--set` re-stages, because config values are inputs to what staging
  generates. `--bind` re-stages only to record the bind against a validated
  manifest -- the links themselves are built here, from whatever binds are on
  file. `bundle` is always required (same as ``stage``); an already-installed
  app still names where it came from, even when start skips restaging.
  """
  paths = ctx.staged_paths(app)

  if sets or binds or not ctx.is_staged(app):
    result = stage(app, bundle, ctx, sets=sets, binds=binds)
  else:
    stack = AppStack.from_file(paths.manifest_path, app)
    result = StageSuccess(stack, load_run_data(stack, ctx))

  stack, run_data = result.stack, result.run_data
  if run_data.start_blockers:
    raise ValueError("\n".join(recovery_lines(app, run_data.start_blockers)))

  if not paths.compose_path.is_file():
    raise ValueError(f"App {app} is not staged; run `harbor stage {app}` first")

  try:
    preflight_app_routes(run_data, ctx)
  except RouteProviderError as e:
    record_app_action("start-failed", app, ctx.config)
    raise ValueError(str(e)) from e

  # The binds only become links here, and only for as long as the app runs.
  # Rebuilt from scratch every time, so a bind recorded since the last start --
  # or since the last stage, which does not touch these -- takes effect now.
  link_ext_volumes(stack, run_data)

  try:
    docker_run_command(
      ["compose", "up", "-d"],
      cwd=paths.run_path,
      json_output=False,
      check=True,
      env=run_data.config_env(),
    )
  except DockerError as e:
    record_app_action("start-failed", app, ctx.config)
    raise ValueError(str(e)) from e

  try:
    register_app_routes(run_data, ctx)
  except RouteProviderError as e:
    record_app_action("start-failed", app, ctx.config)
    raise ValueError(
      f"{e}. Containers may still be running; run `harbor stop {app}` to stop them."
    ) from e

  record_app_action("started", app, ctx.config)
  return result


def _compose_env(app_id: AppID, ctx: HarborCtx) -> dict[str, str]:
  """The config environment compose.yml interpolates `${__HARBOR_CONFIG__*}` from.

  Every compose invocation needs it, not just `up`: without it compose warns
  about each unset variable and renders them blank, so `down` and `logs` would
  be reasoning about a different project definition than `up` created. Best
  effort -- a broken or half-removed app must still be stoppable, so a stack
  that will not parse falls back to no env rather than blocking teardown.
  """
  try:  
    stack = AppStack.from_file(ctx.staged_paths(app_id).manifest_path, app_id)
    return load_run_data(stack, ctx).config_env()
  except ValueError as e:
    logger.debug("no config env for %s: %s", app_id, e)
    return {}


def logs(app_id: AppID, extra_args: list[str], ctx: HarborCtx) -> None:
  """Stream ``docker compose logs`` for a staged app."""
  state = ctx.run_state(app_id)
  if not state.compose_exists:
    raise ValueError(f"App {app_id} is not staged; run `harbor stage {app_id}` first")

  docker_run_command(
    ["compose", "logs", *(extra_args or [])],
    cwd=state.run_path,
    json_output=False,
    check=True,
    env=_compose_env(app_id, ctx),
  )


def stop(app_id: AppID, ctx: HarborCtx) -> None:
  """Tear down routes, then bring an app's containers down."""
  state = ctx.run_state(app_id)
  if not state.compose_exists:
    if state.containers:
      raise ValueError(container_recovery_message(app_id, ctx))
    raise ValueError(f"App {app_id} is not staged; run `harbor stage {app_id}` first")

  try:
    unregister_app_routes(app_id, ctx)
  except Exception as e:
    logger.error("failed to unregister routes for %s: %s", app_id, e)

  try:
    docker_run_command(
      ["compose", "down"],
      cwd=state.run_path,
      json_output=False,
      check=True,
      env=_compose_env(app_id, ctx),
    )
    # Nothing is mounting them now, and leaving them behind is how a stopped
    # app keeps looking like it is still bound to somebody's data.
    unlink_ext_volumes(state.run_path)
    record_app_action("stopped", app_id, ctx.config)
  except DockerError as e:
    record_app_action("stop-failed", app_id, ctx.config)
    raise ValueError(str(e)) from e
