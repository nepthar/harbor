from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Any, TextIO

from harbor.lib.stack import HARBOR_APP_ID_LABEL, HARBOR_RUN_UNIT_LABEL

logger = getLogger("harbor.docker")

DOCKER = "docker"

# Where streamed output goes when the caller has no terminal. Unset, the child
# inherits harbor's stdio.
_output_sink: ContextVar[TextIO | None] = ContextVar("docker_output_sink", default=None)

# How much captured output a DockerError carries. The full text is in the
# sink; the error only needs enough for a one-line failure to make sense.
_ERROR_TAIL = 1500


@contextmanager
def sink_output(sink: TextIO) -> Iterator[None]:
  """Capture streamed docker output into `sink` for the duration."""
  token = _output_sink.set(sink)
  try:
    yield
  finally:
    _output_sink.reset(token)


@dataclass(frozen=True)
class HarborRunUnitStatus:
  app_id: str
  run_unit: str
  container_id: str
  name: str
  state: str


@dataclass(frozen=True)
class DockerReturn:
  """Result of a ``docker`` invocation."""

  returncode: int
  data: list[dict[str, Any]]


def _parse_docker_label_string(raw: str) -> dict[str, str]:
  labels: dict[str, str] = {}
  for part in raw.split(","):
    key, sep, value = part.partition("=")
    if sep:
      labels[key] = value
  return labels


def load_harbor_run_unit_status() -> dict[str, tuple[HarborRunUnitStatus, ...]]:
  """Return every container that carries a Harbor app ID label."""
  result = docker_run_command(
    [
      "ps",
      "-a",
      "--filter",
      f"label={HARBOR_APP_ID_LABEL}",
    ],
    check=False,
  )

  statuses: dict[str, list[HarborRunUnitStatus]] = {}
  for container in result.data:
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

  def __init__(self, cmd: list[str], returncode: int, stderr: str = "") -> None:
    self.cmd = cmd
    self.returncode = returncode
    self.stderr = stderr
    # A streamed command already put its own diagnostics on the terminal, so
    # point at those rather than reporting a bare exit code with no detail.
    detail = stderr.strip() or "see the docker output above"
    super().__init__(f"docker {' '.join(cmd)} failed ({returncode}): {detail}")


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
) -> DockerReturn:
  """Run `docker <cmd>`.

  `json_output` decides both the format and who sees it: True captures and
  parses into `DockerReturn.data`, False lets the child write straight to the
  terminal, because swallowing a minutes-long `compose up` is indistinguishable
  from a hang.
  """
  full = [DOCKER, *cmd]
  if json_output:
    full += ["--format", "json"]

  run_env = {**os.environ, **env} if env else None
  logger.debug("running: %s (cwd=%s)", " ".join(full), cwd)

  sink = _output_sink.get() if not json_output else None
  if sink is not None:
    # Merge stderr so the sink reads the way a terminal would have.
    proc = subprocess.Popen(
      full,
      cwd=cwd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      env=run_env,
    )
    stdout = proc.stdout
    assert stdout is not None
    chunks: list[str] = []
    try:
      while True:
        chunk = stdout.read(4096)
        if not chunk:
          break
        sink.write(chunk)
        sink.flush()
        chunks.append(chunk)
    finally:
      stdout.close()
      proc.wait()
    text = "".join(chunks)
    if check and proc.returncode != 0:
      raise DockerError(cmd, proc.returncode, text[-_ERROR_TAIL:])
    return DockerReturn(returncode=proc.returncode or 0, data=[])

  if not json_output:
    # The child is about to write to the same stdout harbor has been buffering
    # into. Without this, a piped `harbor dev` prints its receipt *after* the
    # compose output it was meant to introduce.
    sys.stdout.flush()
  result = subprocess.run(
    full, cwd=cwd, capture_output=json_output, text=True, env=run_env
  )

  if check and result.returncode != 0:
    raise DockerError(cmd, result.returncode, result.stderr if json_output else "")

  data = _parse_json_output(result.stdout) if json_output else []
  return DockerReturn(returncode=result.returncode, data=data)
