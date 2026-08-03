import logging
from datetime import datetime
from time import time

import requests

from harbor.lib.apps import AppID
from harbor.lib.config import Config
from harbor.lib.store import HarborStore

logger = logging.getLogger("harbor.routes")


class RouteProviderError(Exception):
  """Raised when a route provider cannot complete an operation."""


def refuse_foreign_route(domain_name: str, owner: str | None) -> RouteProviderError:
  owner_desc = f"happ {owner!r}" if owner else "a non-Harbor proxy host"
  return RouteProviderError(
    f"Refusing to replace {domain_name}; it is already owned by {owner_desc}"
  )


class RouteProvider:
  def register_route(
    self,
    app: AppID,
    port: int,
    subdomain: str,
    domain: str,
    scheme: str = "http",
  ):
    """Register a new route with this provider"""
    raise NotImplementedError

  def unregister_route(self, subdomain: str, domain: str):
    """Unregister a route - no longer route to there"""
    raise NotImplementedError

  def list_routes(self) -> list[tuple[str, str]]:
    """Fetch harbor-domain routes as (subdomain, destination) pairs."""
    raise NotImplementedError

  def route_owners(self) -> dict[str, str | None]:
    """Map subdomain under the harbor domain -> owning harbor app id.

    ``None`` means a proxy host exists for that subdomain but is not
    Harbor-owned. Missing keys are free.
    """
    raise NotImplementedError

  def validate(self) -> list[str]:
    """Return an empty list if this is valid, or a list of errors"""
    raise NotImplementedError


class NginxProxyManagerRouteProvider(RouteProvider):
  """Register harbor app routes as proxy hosts in Nginx Proxy Manager.

  Auth follows NPM's token flow: log in with email/password to obtain a bearer
  token, then send it as ``Authorization: Bearer <token>``. The token (and its
  expiry) are cached in the harbordb ``system`` section so we only re-login when
  it is missing or expired.
  """

  # Refresh slightly ahead of the real expiry to avoid racing the clock.
  TOKEN_REFRESH_LEEWAY = 60  # seconds
  # State keys under SystemDB.
  _TOKEN_KEY = "npm_token"
  _TOKEN_EXPIRE_KEY = "npm_token_expire"

  def __init__(
    self,
    endpoint: str,
    email: str,
    password: str,
    harbor_domain: str,
    forward_host: str,
    harbor_db: HarborStore | None = None,
    token: str | None = None,
    token_expire: float | None = None,
    timeout: float = 30.0,
  ):
    self.endpoint = endpoint.rstrip("/")
    self.email = email
    self._password = password
    self.harbor_domain = harbor_domain
    # LAN IP/hostname of the docker host that NPM forwards traffic to. The
    # app's published port is reachable there.
    self.forward_host = forward_host
    self._harbor_db = harbor_db
    self._token = token
    self._token_expire = token_expire or 0.0
    self._timeout = timeout
    self._session = requests.Session()

  # ── auth ──────────────────────────────────────────────────────────────
  def _fetch_token(self) -> str:
    """Return a valid bearer token, logging in (and caching) if needed."""
    if self._token and self._token_expire - self.TOKEN_REFRESH_LEEWAY > time():
      return self._token

    if not self.email or not self._password:
      raise RouteProviderError("NPM email and password are required to authenticate")

    try:
      resp = self._session.post(
        f"{self.endpoint}/api/tokens",
        json={"identity": self.email, "secret": self._password},
        timeout=self._timeout,
      )
    except requests.RequestException as e:
      raise RouteProviderError(f"Could not reach NPM at {self.endpoint}: {e}") from e

    if resp.status_code == 401:
      raise RouteProviderError("NPM rejected the credentials (401)")
    resp.raise_for_status()
    data = resp.json()

    if data.get("requires_2fa"):
      raise RouteProviderError(
        "This NPM account has 2FA enabled, which harbor does not support"
      )

    self._token = data["token"]
    self._token_expire = self._parse_expiry(data["expires"])
    self._cache_token()
    return self._token

  @staticmethod
  def _parse_expiry(expires: str) -> float:
    """NPM returns an ISO-8601 timestamp (e.g. ...Z); store it as epoch seconds."""
    return datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()

  def _cache_token(self) -> None:
    if self._harbor_db is None:
      return
    self._harbor_db.set_token(
      self._TOKEN_KEY, str(self._token), int(self._token_expire)
    )

  # ── low-level request helper ──────────────────────────────────────────

  def _request(self, method: str, path: str, **kwargs):
    url = f"{self.endpoint}{path}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {self._fetch_token()}"
    try:
      resp = self._session.request(
        method, url, headers=headers, timeout=self._timeout, **kwargs
      )
    except requests.RequestException as e:
      raise RouteProviderError(f"NPM {method} {path} failed: {e}") from e

    if not resp.ok:
      raise RouteProviderError(
        f"NPM {method} {path} returned {resp.status_code}: {resp.text}"
      )
    return resp.json() if resp.content else None

  # ── certificates ──────────────────────────────────────────────────────

  def _wildcard_certificate_id(self) -> int | None:
    """Return the id of a cert covering ``*.harbor_domain``, if one exists."""
    wildcard = f"*.{self.harbor_domain}"
    for cert in self._request("GET", "/api/nginx/certificates") or []:
      if wildcard in cert.get("domain_names", []):
        return cert["id"]
    return None

  # ── proxy hosts ───────────────────────────────────────────────────────

  def _find_proxy_host(self, domain_name: str) -> dict | None:
    for host in self._request("GET", "/api/nginx/proxy-hosts") or []:
      if domain_name in host.get("domain_names", []):
        return host
    return None

  def _domain_name(self, subdomain: str | None, domain: str) -> str:
    return f"{subdomain}.{domain}" if subdomain else domain

  # ── RouteProvider interface ───────────────────────────────────────────

  def validate(self) -> list[str]:
    try:
      self._fetch_token()
    except RouteProviderError as e:
      return [str(e)]

    errors: list[str] = []
    try:
      if self._wildcard_certificate_id() is None:
        errors.append(f"NPM has no wildcard certificate for *.{self.harbor_domain}")
    except RouteProviderError as e:
      errors.append(f"Could not list NPM certificates: {e}")
    return errors

  def register_route(
    self,
    app: AppID,
    port: int,
    subdomain: str,
    domain: str,
    scheme: str = "http",
  ):
    """Create (or update) a proxy host pointing domain -> forward_host:port.

    Idempotent: if a proxy host already serves this domain we update it in
    place so re-running ``start_app`` does not create duplicates.
    """
    domain_name = self._domain_name(subdomain, domain)
    cert_id = self._wildcard_certificate_id()
    if cert_id is None:
      raise RouteProviderError(
        f"NPM has no wildcard certificate for *.{self.harbor_domain}; "
        f"refusing to publish {domain_name}"
      )

    payload = {
      "domain_names": [domain_name],
      "forward_scheme": scheme,
      "forward_host": self.forward_host,
      "forward_port": port,
      "access_list_id": 0,
      "certificate_id": cert_id,
      "ssl_forced": True,
      "http2_support": True,
      "hsts_enabled": False,
      "hsts_subdomains": False,
      "block_exploits": True,
      "caching_enabled": False,
      "allow_websocket_upgrade": True,
      "advanced_config": "",
      "locations": [],
      "meta": {"harbor_app": app},
    }

    existing = self._find_proxy_host(domain_name)
    if existing:
      owner = (existing.get("meta") or {}).get("harbor_app")
      if owner != app:
        raise refuse_foreign_route(domain_name, owner)
      logger.info(
        "updating NPM proxy host for %s -> %s:%d", domain_name, self.forward_host, port
      )
      self._request("PUT", f"/api/nginx/proxy-hosts/{existing['id']}", json=payload)
    else:
      logger.info(
        "creating NPM proxy host for %s -> %s:%d", domain_name, self.forward_host, port
      )
      self._request("POST", "/api/nginx/proxy-hosts", json=payload)

  def unregister_route(self, subdomain: str, domain: str):
    domain_name = self._domain_name(subdomain, domain)
    existing = self._find_proxy_host(domain_name)
    if existing:
      logger.info("deleting NPM proxy host for %s", domain_name)
      self._request("DELETE", f"/api/nginx/proxy-hosts/{existing['id']}")

  def _harbor_proxy_hosts(self) -> list[tuple[str, dict]]:
    """Yield (subdomain, host) for single-label hosts under the harbor domain.

    Only ``<label>.<harbor_domain>`` counts — multi-label names like
    ``qbt.arr.<domain>`` are other systems' routes and are ignored.
    """
    suffix = f".{self.harbor_domain}"
    out: list[tuple[str, dict]] = []
    for host in self._request("GET", "/api/nginx/proxy-hosts") or []:
      for dn in host.get("domain_names", []):
        if not dn.endswith(suffix):
          continue
        subdomain = dn[: -len(suffix)]
        if not subdomain or "." in subdomain:
          continue
        out.append((subdomain, host))
    return out

  def list_routes(self) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for subdomain, host in self._harbor_proxy_hosts():
      dest = f"{host.get('forward_host', '?')}:{host.get('forward_port', '?')}"
      routes.append((subdomain, dest))
    return routes

  def route_owners(self) -> dict[str, str | None]:
    return {
      subdomain: (host.get("meta") or {}).get("harbor_app")
      for subdomain, host in self._harbor_proxy_hosts()
    }


class NoopRouteProvider(RouteProvider):
  """The "unconfigured" route provider - just loggs that a route provider is not configured."""

  def __init__(self):
    self.routes: dict[str, str] = {}
    self.owners: dict[str, str] = {}

  def register_route(
    self,
    app: AppID,
    port: int,
    subdomain: str,
    domain: str,
    scheme: str = "http",
  ):
    route = f"{subdomain}.{domain}"
    owner = self.owners.get(subdomain)
    if owner is not None and owner != app:
      raise refuse_foreign_route(route, owner)
    self.routes[subdomain] = f":{port}"
    self.owners[subdomain] = app
    logger.warning(f"No route provider configured - {app} has requested {route}")

  def unregister_route(self, subdomain: str, domain: str):
    route = f"{subdomain}.{domain}"
    logger.warning(f"No route provider configured - Attempting to unregister {route}")
    self.routes.pop(subdomain, None)
    self.owners.pop(subdomain, None)

  def list_routes(self) -> list[tuple[str, str]]:
    return sorted(self.routes.items())

  def route_owners(self) -> dict[str, str | None]:
    return dict(self.owners)

  def validate(self) -> list[str]:
    return []


class PangolinRouteProvider(RouteProvider):
  ## Ignore for now
  pass


def get_route_provider(harbor_db: HarborStore, config: Config) -> RouteProvider:
  """Build the configured route provider, or a no-op provider when unconfigured.

  Reads ``[route_provider.<kind>]`` from the Harbor config.
  ``password_secret`` names an encrypted secret in the harbordb system section.
  """

  providers = config.route_provider
  if len(providers) != 1:
    logger.warning(
      "Exactly one route provider should be configured, there are %d. "
      "Using dummy provider",
      len(providers),
    )
    return NoopRouteProvider()

  name, conf = list(providers.items())[0]

  if name == "nginx_proxy_manager":
    reqd = ("endpoint", "email", "password_secret", "forward_host")

    if any(r not in conf for r in reqd):
      raise RouteProviderError(f"Missing configuration in route_provider. Needs {reqd}")

    pw_ref = conf["password_secret"]
    password = harbor_db.get_secret(conf["password_secret"])
    if not password:
      raise RouteProviderError(
        f"Missing password secret {pw_ref!r}. Run: harbor config-sys --stdin {pw_ref}"
      )

    tok, exp = harbor_db.get_token(NginxProxyManagerRouteProvider._TOKEN_KEY)

    return NginxProxyManagerRouteProvider(
      endpoint=conf["endpoint"],
      email=conf["email"],
      password=password,
      harbor_domain=config.domain,
      forward_host=conf["forward_host"],
      harbor_db=harbor_db,
      token=tok,
      token_expire=exp,
    )

  logger.warning(f"Unable to find a route provider for {name}. Using dummy provider.")
  return NoopRouteProvider()
