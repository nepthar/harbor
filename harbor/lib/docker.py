from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Any

from harbor.lib.stack import HARBOR_APP_ID_LABEL, HARBOR_RUN_UNIT_LABEL

logger = getLogger("harbor.docker")

DOCKER = "docker"


@dataclass(frozen=True)
class HarborRunUnitStatus:
  app_id: str
  run_unit: str
  container_id: str
  name: str
  state: str


def _parse_docker_label_string(raw: str) -> dict[str, str]:
  labels: dict[str, str] = {}
  for part in raw.split(","):
    key, sep, value = part.partition("=")
    if sep:
      labels[key] = value
  return labels


def load_harbor_run_unit_status() -> dict[str, tuple[HarborRunUnitStatus, ...]]:
  """Return every container that carries a Harbor app ID label."""
  containers = docker_run_command(
    [
      "ps",
      "-a",
      "--filter",
      f"label={HARBOR_APP_ID_LABEL}",
    ],
    check=False,
  )
  if not isinstance(containers, list):
    return {}

  statuses: dict[str, list[HarborRunUnitStatus]] = {}
  for container in containers:
    labels = _parse_docker_label_string(container.get("Labels") or "")
    app_id = labels.get(HARBOR_APP_ID_LABEL)
    if not app_id:
      continue
    run_unit = labels.get(HARBOR_RUN_UNIT_LABEL, "")

    app_units = statuses.setdefault(app_id, [])
    app_units.append(
      HarborRunUnitStatus(
        app_id=app_id,
        run_unit=run_unit,
        container_id=container.get("ID", ""),
        name=container.get("Names", ""),
        state=container.get("State", ""),
      )
    )
  return {app_id: tuple(app_units) for app_id, app_units in statuses.items()}


class DockerError(RuntimeError):
  """A docker invocation exited non-zero."""

  def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
    self.cmd = cmd
    self.returncode = returncode
    self.stderr = stderr
    super().__init__(f"docker {' '.join(cmd)} failed ({returncode}): {stderr.strip()}")


def _parse_json_output(stdout: str) -> list[dict[str, Any]]:
  """Parse docker JSON output, tolerating both a single array and NDJSON lines."""
  text = stdout.strip()
  if not text:
    return []
  try:
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]
  except json.JSONDecodeError:
    # Fall back to newline-delimited JSON (one object per line).
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def docker_run_command(
  cmd: list[str],
  *,
  cwd: Path | None = None,
  json_output: bool = True,
  check: bool = True,
  env: dict[str, str] | None = None,
  capture: bool = True,
) -> list[dict[str, Any]] | str:
  """Run ``docker <cmd>`` and return its output.

  Args:
      cmd: arguments after ``docker`` (e.g. ``["compose", "ps"]``).
      cwd: working directory for the command (e.g. a run path).
      json_output: when True, append ``--format json`` and parse the result into a
          ``list[dict]``; when False, return the raw stdout string.
      check: raise :class:`DockerError` on a non-zero exit.
      env: extra environment variables merged onto ``os.environ``.
      capture: when True, capture stdout/stderr; when False, inherit the parent's
          stdio so output streams straight to the terminal (e.g. ``compose logs -f``)
          and return an empty string.

  Returns:
      ``list[dict]`` when ``json_output`` is True, else the raw stdout ``str``.
  """
  full = [DOCKER, *cmd]
  if json_output:
    full += ["--format", "json"]

  run_env = {**os.environ, **env} if env else None
  logger.debug("running: %s (cwd=%s)", " ".join(full), cwd)
  result = subprocess.run(full, cwd=cwd, capture_output=capture, text=True, env=run_env)

  if check and result.returncode != 0:
    raise DockerError(cmd, result.returncode, result.stderr if capture else "")

  if not capture:
    return ""
  return _parse_json_output(result.stdout) if json_output else result.stdout
