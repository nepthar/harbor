"""Talk to harbord. The only I/O this app does besides serving pages."""

import errno
import http.client
import json
import os
import socket

SOCKET = os.environ.get("HARBOR_SOCKET", "/harbor/conn/admin.sock")
# host:port wins over the socket when set. Docker Desktop's bind mounts cannot
# carry AF_UNIX, so a mac host serves this over TCP instead.
API = os.environ.get("HARBOR_API", "").strip()


class UnixHTTPConnection(http.client.HTTPConnection):
  """http.client over AF_UNIX. The Host header is a formality here."""

  def __init__(self, path, timeout=10):
    super().__init__("localhost", timeout=timeout)
    self._unix_path = path

  def connect(self):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(self.timeout)
    sock.connect(self._unix_path)
    self.sock = sock


class ApiError(Exception):
  """harbord could not be reached, or refused. Rendered, never raised at a user."""


def where():
  """Human-readable name for whichever transport is in play."""
  return API if API else SOCKET


def connect(timeout=10):
  if not API:
    return UnixHTTPConnection(SOCKET, timeout=timeout)
  address = API.split("://", 1)[-1].rstrip("/")
  host, _, port = address.rpartition(":")
  if not port.isdigit():
    raise ApiError(f"HARBOR_API must be host:port, got {API!r}")
  return http.client.HTTPConnection(host or "127.0.0.1", int(port), timeout=timeout)


def api(path, method="GET", payload=None, timeout=10):
  conn = connect(timeout)
  body = json.dumps(payload).encode() if payload is not None else None
  headers = {"Content-Type": "application/json"} if body else {}
  try:
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    status = response.status
  except FileNotFoundError as e:
    raise ApiError(
      f"No socket at {SOCKET}. Is harbord running, and is $harbor/var/conn bound "
      f"into this container?"
    ) from e
  except OSError as e:
    hint = ""
    # EOPNOTSUPP on a socket that is plainly there is Docker Desktop: its bind
    # mounts cannot carry AF_UNIX, and no amount of rebinding will fix it.
    if not API and e.errno == errno.EOPNOTSUPP:
      hint = (
        " This host's bind mounts cannot carry a unix socket (Docker Desktop "
        "does not support it). Run `harbord --port N --host 0.0.0.0` and set "
        "`harbor config harbor-ui --set api_address=host.docker.internal:N`."
      )
    raise ApiError(f"Cannot reach harbord at {where()}: {e}.{hint}") from e
  finally:
    conn.close()

  try:
    body = json.loads(raw)
  except ValueError as e:
    raise ApiError(f"harbord sent something that is not JSON ({status})") from e
  if status >= 400:
    raise ApiError(str(body.get("error", body)))
  return body
