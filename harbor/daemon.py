"""harbord — the harbor admin API over a unix socket.

harbord is a second front door, not a layer under the CLI. Both call the same
`harbor.lib` functions and serialize against each other with the same lock
file, so neither is a client of the other and `harbor` keeps working whether
or not this is running.

The transport is stdlib http.server on purpose. The API is small, the traffic
is one operator, and the alternative was an ASGI stack in a process that runs
as root. `harbor.lib.api.dispatch` holds every route, so replacing this file
is the only cost of outgrowing it.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

from harbor import VERSION
from harbor.lib.api import dispatch
from harbor.lib.config import Config, load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.jobs import JobRunner

logger = logging.getLogger("harbord")

# Owner and group only. The group is the whole access control story: anything
# that can open this socket can run every verb the API exposes, which is why
# the API exposes no verb that can define a new app.
SOCKET_MODE = 0o660

MAX_BODY = 1 << 20

# sun_path is 104 bytes on macOS and 108 on Linux. Binding past it fails with
# "AF_UNIX path too long", which says nothing about which path or what to do.
MAX_SOCKET_PATH = 104


class _Handler(http.server.BaseHTTPRequestHandler):
  server_version = f"harbord/{VERSION}"

  # Set by `_make_server`.
  ctx_factory = staticmethod(lambda: None)
  jobs: JobRunner

  def do_GET(self) -> None:
    self._respond("GET", None)

  def do_POST(self) -> None:
    length = int(self.headers.get("Content-Length") or 0)
    if length > MAX_BODY:
      self._write(413, {"error": f"Body exceeds {MAX_BODY} bytes"})
      return
    raw = self.rfile.read(length) if length else b""
    try:
      body = json.loads(raw) if raw else None
    except json.JSONDecodeError as e:
      self._write(400, {"error": f"Body is not valid JSON: {e}"})
      return
    self._respond("POST", body)

  def _respond(self, method: str, body: dict | None) -> None:
    try:
      response = dispatch(method, self.path, body, self.ctx_factory, self.jobs)
    except (ValueError, RuntimeError) as e:
      # Config that stopped loading, a vanished harbor root: real, and not the
      # caller's fault. Everything a caller can provoke is already a Response.
      logger.exception("request failed: %s %s", method, self.path)
      self._write(500, {"error": str(e)})
      return
    self._write(response.status, response.body)

  def _write(self, status: int, body: object) -> None:
    payload = json.dumps(body, indent=2).encode() + b"\n"
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)

  def address_string(self) -> str:
    # A unix peer has no address, and the base class would index an empty one.
    return "unix"

  def log_message(self, format: str, *args) -> None:
    logger.info("%s %s", self.address_string(), format % args)


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
  address_family = socket.AF_UNIX
  daemon_threads = True

  def server_bind(self) -> None:
    # HTTPServer.server_bind derives a name and port from the address; a unix
    # socket has neither, so bind like a plain TCPServer and name it here.
    socketserver.TCPServer.server_bind(self)
    self.server_name = "harbord"
    self.server_port = 0


class _TCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
  daemon_threads = True
  allow_reuse_address = True


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


def _make_server(server_class, address, config_args, jobs: JobRunner):
  def ctx_factory() -> HarborCtx:
    # Config is re-read per request rather than held: harbor's state is the
    # filesystem, and a request is the daemon's equivalent of an invocation.
    config = load_config(**config_args)
    if config is None:
      raise RuntimeError("Harbor is not initialized; run `harbor init` first")
    return HarborCtx(config)

  handler = type(
    "_BoundHandler",
    (_Handler,),
    {"ctx_factory": staticmethod(ctx_factory), "jobs": jobs},
  )
  return server_class(address, handler)


def serve(
  config: Config,
  config_args: dict[str, str | None],
  *,
  socket_path: Path | None = None,
  port: int | None = None,
) -> None:
  socket_path = socket_path or config.admin_socket_path
  jobs = JobRunner(
    lambda: HarborCtx(load_config(**config_args) or config)  # type: ignore[arg-type]
  )
  jobs.start()

  _claim_socket_path(socket_path)
  unix_server = _make_server(_UnixHTTPServer, str(socket_path), config_args, jobs)
  os.chmod(socket_path, SOCKET_MODE)
  logger.warning("harbord %s listening on %s", VERSION, socket_path)

  tcp_server = None
  if port is not None:
    tcp_server = _make_server(_TCPServer, ("127.0.0.1", port), config_args, jobs)
    logger.warning("harbord also listening on http://127.0.0.1:%d", port)
    threading.Thread(
      target=tcp_server.serve_forever, name="harbord-tcp", daemon=True
    ).start()

  try:
    unix_server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    unix_server.server_close()
    if tcp_server is not None:
      tcp_server.shutdown()
      tcp_server.server_close()
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
  except (ValueError, RuntimeError, OSError) as error:
    # OSError covers the socket itself: a path harbord cannot write, a
    # directory it cannot create, a bind that the kernel refuses.
    print(f"Error: {error}", file=sys.stderr)
    raise SystemExit(1) from error
