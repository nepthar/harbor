"""harbord — the harbor admin API over a unix socket.

harbord is a second front door, not a layer under the CLI. Both call the same
`harbor.lib` functions and serialize against each other with the same lock
file, so neither is a client of the other and `harbor` keeps working whether
or not this is running.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

from harbor import VERSION
from harbor.daemon.jobs import JobRunner
from harbor.lib.config import Config, load_config
from harbor.lib.harbor import HarborCtx

try:
  import uvicorn

  from harbor.daemon.api import create_app
except ImportError as e:  # pragma: no cover - depends on how harbor was installed
  raise ImportError(
    "harbord needs starlette and uvicorn, which a plain harbor install does "
    "not pull in. Install them with: uv tool install 'harbor[daemon]'"
  ) from e

logger = logging.getLogger("harbord")

# Owner and group only. The group is the whole access-control story: anything
# that can open this socket can run every verb the API exposes, which is why
# the API exposes no verb that can define a new app.
SOCKET_MODE = 0o660

# sun_path is 104 bytes on macOS and 108 on Linux. Binding past it fails with
# "AF_UNIX path too long", which says nothing about which path or what to do.
MAX_SOCKET_PATH = 104

BACKLOG = 128


def _claim_socket_path(path: Path) -> None:
  """Make `path` free to bind, or refuse and say who has it.

  A socket file left by a killed harbord is removed; one that still answers is
  another harbord, and taking it over would silently steal its traffic.
  """
  if len(str(path).encode()) > MAX_SOCKET_PATH:
    raise RuntimeError(
      f"Socket path is {len(str(path).encode())} bytes, over the "
      f"{MAX_SOCKET_PATH}-byte limit the OS allows: {path}\n"
      f"Pass --socket with a shorter path, or move the harbor root."
    )
  # Only tighten a directory harbord created. A `--socket` pointed into an
  # existing shared directory is the operator's to set up, not ours to chmod.
  if not path.parent.exists():
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o750)
  if not path.exists():
    return

  probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  try:
    probe.settimeout(1.0)
    probe.connect(str(path))
  except OSError:
    logger.warning("removing stale socket at %s", path)
    path.unlink()
    return
  finally:
    probe.close()

  raise RuntimeError(
    f"Another harbord is already listening on {path}. "
    f"Stop it first, or pass --socket to use a different path."
  )


def _bind_unix(path: Path) -> socket.socket:
  """Bind the admin socket ourselves so it is never briefly world-writable --
  uvicorn's own `uds` handling chmods it to 0666."""
  _claim_socket_path(path)
  sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  sock.bind(str(path))
  os.chmod(path, SOCKET_MODE)
  sock.listen(BACKLOG)
  return sock


def _bind_tcp(port: int) -> socket.socket:
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  sock.bind(("127.0.0.1", port))
  sock.listen(BACKLOG)
  return sock


def serve(
  config: Config,
  config_args: dict[str, str | None],
  *,
  socket_path: Path | None = None,
  port: int | None = None,
) -> None:
  socket_path = socket_path or config.admin_socket_path

  def ctx_factory() -> HarborCtx:
    # Config is re-read per request rather than held: harbor's state is the
    # filesystem, and nothing should survive a `harbor config` edit.
    loaded = load_config(**config_args)
    if loaded is None:
      raise RuntimeError("Harbor is not initialized; run `harbor init` first")
    return HarborCtx(loaded)

  jobs = JobRunner(ctx_factory)
  jobs.start()

  sockets = [_bind_unix(socket_path)]
  logger.warning("harbord %s listening on %s", VERSION, socket_path)
  if port is not None:
    sockets.append(_bind_tcp(port))
    logger.warning("harbord also listening on http://127.0.0.1:%d", port)

  server = uvicorn.Server(
    uvicorn.Config(
      create_app(ctx_factory, jobs),
      # harbor configures its own logging; uvicorn's would replace it.
      log_config=None,
      access_log=False,
    )
  )
  try:
    server.run(sockets=sockets)
  finally:
    socket_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="harbord", description="Harbor admin daemon")
  parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
  parser.add_argument("--root", metavar="DIR", help="Harbor root directory")
  parser.add_argument("--config", metavar="FILE", help="Path to config.toml")
  parser.add_argument(
    "--socket",
    metavar="PATH",
    help="Admin socket path (default: <harbor_root>/conn/admin.sock)",
  )
  parser.add_argument(
    "--port",
    type=int,
    metavar="N",
    help="Also listen on 127.0.0.1:N, for reaching the API over an ssh tunnel",
  )
  return parser


def main() -> None:
  logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
  )
  args = build_parser().parse_args()
  config_args = {"config_path": args.config, "root": args.root}

  try:
    config = load_config(**config_args)
    if config is None:
      raise RuntimeError("Harbor is not initialized; run `harbor init` first")
    serve(
      config,
      config_args,
      socket_path=Path(args.socket).expanduser() if args.socket else None,
      port=args.port,
    )
  except KeyboardInterrupt:
    pass
  except (ValueError, RuntimeError, OSError) as error:
    # OSError covers the socket itself: a path harbord cannot write, a
    # directory it cannot create, a bind the kernel refuses.
    print(f"Error: {error}", file=sys.stderr)
    raise SystemExit(1) from error
