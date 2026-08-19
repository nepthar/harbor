"""Read and write root-owned volume files without any host privilege.

Containers write their volume files as root, so the host process cannot copy,
archive, or delete them. Harbor used to shell out to `sudo` for this, which
needs a TTY for the password prompt and so cannot run unattended. Docker is
already a hard requirement and its containers already run as root, so a
throwaway container with the right binds does the same work with no host
privilege at all.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from logging import getLogger
from pathlib import Path
from typing import IO

from harbor.lib.docker import DOCKER

logger = getLogger("harbor.lifecycle.rootfs")

# Pinned like every other image harbor names. busybox's sh, tar, cp and rm are
# the whole toolbox this needs.
ROOTFS_IMAGE = "alpine:3.22"


def run_as_root(
  what: str,
  script: str,
  mounts: Iterable[Path],
  *,
  stdout: IO[bytes] | None = None,
) -> None:
  """Run `sh -c script` in a throwaway container over the given host paths.

  Each mount is bound at its own absolute host path, so `script` names paths
  exactly as the host does. Mounts must therefore already be resolved, and the
  paths inside `script` resolved the same way, or the two will not agree.

  `stdout` is a file *this* process opened: the container writes a stream, the
  host owns the result. That is how the snapshot archive stays owned by the
  invoking user instead of root.

  `what` completes "Unable to ..." in the error raised on a non-zero exit.
  """
  # dict, not set: docker rejects the same path bound twice, and the argument
  # order has to be stable for a test to assert on the command.
  resolved = dict.fromkeys(str(mount.resolve()) for mount in mounts)

  # `-v host:guest` is colon-delimited, so a colon in a path silently becomes
  # a different bind rather than an error. Harbor does not allow one anywhere
  # it puts files, so refuse here rather than mount something unintended.
  for path in resolved:
    if ":" in path:
      raise ValueError(
        f"Harbor cannot use a path containing a colon: {path}\n"
        f"Rename it, or point the matching root in config.toml somewhere else."
      )
  binds = [arg for path in resolved for arg in ("-v", f"{path}:{path}")]

  cmd = [DOCKER, "run", "--rm", *binds, ROOTFS_IMAGE, "sh", "-c", script]

  # At warning level because this is the only sign of life during a step that
  # can take minutes -- a big volume to copy, or a first-run image pull. The
  # sudo prompt this replaced used to serve that purpose by accident.
  logger.warning("Using a throwaway %s container to %s", ROOTFS_IMAGE, what)
  logger.debug("running as root in a container: %s", " ".join(cmd))
  # stderr is captured rather than streamed so it can go into the error below.
  # The cost is that a first-run image pull is silent, which alpine's few MB
  # make tolerable; a bigger image would need the streaming treatment
  # `docker_run_command` gives `compose up`.
  result = subprocess.run(cmd, stdout=stdout, stderr=subprocess.PIPE)

  if result.returncode != 0:
    detail = (result.stderr or b"").decode(errors="replace").strip()
    message = (
      f"Unable to {what}. Harbor does this in a throwaway {ROOTFS_IMAGE} "
      f"container because containers write volume files as root; that "
      f"container exited {result.returncode}. "
      f"Check the daemon with `docker info`."
    )
    raise RuntimeError(f"{message}\n{detail}" if detail else message)
