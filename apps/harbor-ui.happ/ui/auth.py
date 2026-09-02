"""One password, one signed cookie. The whole of this app's authentication.

There are no accounts and no session store. The password is harbor config
(`admin_pass`), and the cookie is signed with a key derived from it, so
resetting the password invalidates every session issued under the old one.
"""

import hashlib
import hmac
import os
import time

COOKIE = "harbor_session"
SESSION_SECONDS = 7 * 24 * 3600

PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

if not PASSWORD:
  raise RuntimeError(
    "ADMIN_PASSWORD is empty. Set it with: "
    "harbor config harbor-ui --set admin_pass=<password>"
  )

_KEY = hmac.new(b"harbor-ui/session/v1", PASSWORD.encode(), hashlib.sha256).digest()


def _sign(payload):
  return hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()


def check_password(candidate):
  """True if this is the admin password. Guessing is the rate limiter's problem."""
  return hmac.compare_digest(
    hashlib.sha256(candidate.encode()).digest(),
    hashlib.sha256(PASSWORD.encode()).digest(),
  )


def issue():
  """A fresh cookie value and how many seconds it is good for."""
  expiry = int(time.time()) + SESSION_SECONDS
  return f"{expiry}.{_sign(str(expiry))}", SESSION_SECONDS


def valid(cookie):
  if not cookie:
    return False
  payload, _, signature = cookie.partition(".")
  if not payload.isdigit() or not hmac.compare_digest(signature, _sign(payload)):
    return False
  return int(payload) > int(time.time())


def is_open(path):
  """Paths served without a session: the login form and its stylesheet."""
  return path == "/login" or path.startswith("/static/")
