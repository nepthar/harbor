import logging

import requests

from harbor.lib.apps import AppID
from harbor.lib.config import Config, RouteProviderEntry
from harbor.lib.store import HarborStore

from .base import RouteProvider, RouteProviderError, refuse_foreign_route

logger = logging.getLogger("harbor.routes")


class PangolinRouteProvider(RouteProvider):
  """Register harbor app routes as public HTTP resources in Pangolin.

  Auth is a static integration-API key sent as ``Authorization: Bearer <key>``.
  There is no login flow and nothing to cache, so unlike NPM this provider does
  not touch harbordb. That key goes out on every request, so the endpoint must
  be https and a plaintext one is refused rather than downgraded to.

  A harbor route becomes one Pangolin resource (the hostname, TLS and access
  policy) plus one target under it (``harbor_address:port`` on the configured
  site). Pangolin resources carry no free-form metadata, so ownership is
  recorded in the resource's display name as ``harbor:<app id>``; a resource
  whose name lacks that prefix reads as un-owned and is never replaced.
  """

  KIND = "pangolin"
  REQUIRED_ARGS = ("endpoint", "org_id", "site", "api_key_secret")

  # Pangolin's integration API is versioned in the path, not the host.
  API_PREFIX = "/v1"
  # Ownership marker, in lieu of a metadata field. See the class docstring.
  NAME_PREFIX = "harbor:"

  @classmethod
  def from_config(
    cls,
    tag: str,
    conf: RouteProviderEntry,
    config: Config,
    harbor_db: HarborStore,
  ) -> "PangolinRouteProvider":
    args = cls._args(tag, conf)
    api_key = cls._secret(harbor_db, args.pop("api_key_secret"))
    return cls(
      api_key=api_key,
      harbor_domain=conf.domain,
      harbor_address=config.harbor_address,
      **args,
    )

  def __init__(
    self,
    endpoint: str,
    api_key: str,
    org_id: str,
    site: str,
    harbor_domain: str,
    harbor_address: str,
    timeout: float = 30.0,
  ):
    self.endpoint = self._https_endpoint(endpoint)
    self._api_key = api_key
    self.org_id = org_id
    # The niceId of the Pangolin site (newt tunnel or local site) that can
    # reach harbor. Resolved to the numeric id targets need; see _site_id.
    self.site = site
    self._resolved_site_id: int | None = None
    self.harbor_domain = harbor_domain
    # LAN IP/hostname of the docker host Pangolin forwards traffic to. The
    # app's published port is reachable there.
    self.harbor_address = harbor_address
    self._timeout = timeout
    self._session = requests.Session()

  @staticmethod
  def _https_endpoint(endpoint: str) -> str:
    """Refuse anything but https, rather than quietly upgrading to it.

    The API key is a bearer token on every call, so a plaintext endpoint hands
    it to anyone on the path -- and an operator who wrote http:// has a
    Pangolin that needs fixing, not a URL harbor should rewrite behind them.
    """
    endpoint = endpoint.rstrip("/")
    if not endpoint.lower().startswith("https://"):
      raise RouteProviderError(
        f"Pangolin endpoint {endpoint!r} is not https. Harbor sends the API key "
        f"as a bearer token on every request and will not do that in the clear; "
        f"set args.endpoint to https://<host> and serve the API over TLS"
      )
    return endpoint

  # ── low-level request helper ──────────────────────────────────────────

  def _request(self, method: str, path: str, **kwargs):
    """Call the integration API and unwrap Pangolin's response envelope.

    Every response is ``{data, success, error, message, status}``; callers only
    ever want ``data``.
    """
    if not self._api_key:
      raise RouteProviderError("A Pangolin API key is required to authenticate")

    url = f"{self.endpoint}{self.API_PREFIX}{path}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {self._api_key}"
    try:
      resp = self._session.request(
        method, url, headers=headers, timeout=self._timeout, **kwargs
      )
    except requests.RequestException as e:
      raise RouteProviderError(f"Pangolin {method} {path} failed: {e}") from e

    if resp.status_code in (401, 403):
      raise RouteProviderError(
        f"Pangolin rejected the API key ({resp.status_code}); check that it is "
        f"valid and scoped to org {self.org_id!r}"
      )

    if not resp.ok:
      raise RouteProviderError(
        f"Pangolin {method} {path} returned {resp.status_code}: {resp.text}"
      )

    body = resp.json() if resp.content else {}
    return body.get("data") if isinstance(body, dict) else body

  # ── sites ─────────────────────────────────────────────────────────────

  def _site_id(self) -> int:
    """The numeric siteId behind the configured niceId.

    Targets are created against a numeric id that the dashboard never shows,
    so config names the site the only way an operator can read it off Pangolin
    -- the niceId in the site's URL, e.g.
    ``.../sites/substantial-atractaspis-branchi/general``. Resolved once and
    kept for the life of the provider.
    """
    if self._resolved_site_id is None:
      data = self._request("GET", f"/org/{self.org_id}/site/{self.site}") or {}
      site_id = data.get("siteId")
      if site_id is None:
        raise RouteProviderError(
          f"Pangolin org {self.org_id!r} has no site {self.site!r}; use the name "
          f"from the site's URL in the Pangolin dashboard"
        )
      self._resolved_site_id = int(site_id)
    return self._resolved_site_id

  # ── domains ───────────────────────────────────────────────────────────

  def _domain(self) -> dict | None:
    """Return the org domain entry whose base domain is the harbor domain."""
    data = self._request("GET", f"/org/{self.org_id}/domains") or {}
    for domain in data.get("domains", []):
      if domain.get("baseDomain") == self.harbor_domain:
        return domain
    return None

  def _domain_id(self) -> str:
    domain = self._domain()
    if domain is None:
      raise RouteProviderError(
        f"Pangolin org {self.org_id!r} has no domain {self.harbor_domain!r}; "
        f"add it in Pangolin before publishing routes there"
      )
    return domain["domainId"]

  # ── resources ─────────────────────────────────────────────────────────

  def _resources(self) -> list[dict]:
    # pageSize defaults to 20; ask for the lot so paging never hides a route.
    data = (
      self._request(
        "GET", f"/org/{self.org_id}/public-resources", params={"pageSize": 1000}
      )
      or {}
    )
    return data.get("resources", [])

  def _find_resource(self, domain_name: str) -> dict | None:
    for resource in self._resources():
      if resource.get("fullDomain") == domain_name:
        return resource
    return None

  def _owner(self, resource: dict) -> str | None:
    name = resource.get("name") or ""
    return name[len(self.NAME_PREFIX) :] if name.startswith(self.NAME_PREFIX) else None

  def _targets(self, resource: dict) -> list[dict]:
    """Targets under a resource, using the copy the list endpoint embeds."""
    embedded = resource.get("targets")
    if embedded is not None:
      return embedded
    rid = resource["resourceId"]
    data = self._request("GET", f"/public-resource/{rid}/targets") or {}
    return data.get("targets", [])

  def _set_target(self, resource: dict, port: int, scheme: str) -> None:
    """Point the resource at ``harbor_address:port``, replacing any targets.

    Harbor owns the whole resource, so the target set is whatever we last
    wrote. Clearing and recreating keeps a re-registered route from stacking
    up stale targets that Pangolin would then load-balance across.
    """
    rid = resource["resourceId"]
    for target in self._targets(resource):
      self._request("DELETE", f"/target/{target['targetId']}")

    self._request(
      "PUT",
      f"/public-resource/{rid}/target",
      json={
        "siteId": self._site_id(),
        "ip": self.harbor_address,
        "port": port,
        "method": scheme,
        "mode": "http",
        "enabled": True,
      },
    )

  def _domain_name(self, subdomain: str | None, domain: str) -> str:
    return f"{subdomain}.{domain}" if subdomain else domain

  # ── RouteProvider interface ───────────────────────────────────────────

  def validate(self) -> list[str]:
    try:
      domain = self._domain()
    except RouteProviderError as e:
      return [str(e)]

    errors: list[str] = []
    if domain is None:
      errors.append(
        f"Pangolin org {self.org_id!r} has no domain {self.harbor_domain!r}"
      )
    elif not domain.get("verified", True):
      errors.append(f"Pangolin domain {self.harbor_domain} is not verified yet")

    try:
      self._request("GET", f"/site/{self._site_id()}")
    except RouteProviderError as e:
      errors.append(f"Pangolin site {self.site!r} is not usable: {e}")

    return errors

  def register_route(
    self,
    app: AppID,
    port: int,
    subdomain: str,
    domain: str,
    scheme: str = "http",
  ):
    """Create (or update) a resource routing domain -> harbor_address:port.

    Idempotent: if a resource already serves this domain we reuse it and just
    re-point its target, so re-running ``start_app`` does not create duplicates.
    """
    domain_name = self._domain_name(subdomain, domain)
    existing = self._find_resource(domain_name)

    if existing:
      owner = self._owner(existing)
      if owner != app:
        raise refuse_foreign_route(domain_name, owner)
      logger.info(
        "updating Pangolin resource for %s -> %s:%d",
        domain_name,
        self.harbor_address,
        port,
      )
      resource = existing
    else:
      logger.info(
        "creating Pangolin resource for %s -> %s:%d",
        domain_name,
        self.harbor_address,
        port,
      )
      resource = self._request(
        "PUT",
        f"/org/{self.org_id}/public-resource",
        json={
          "name": f"{self.NAME_PREFIX}{app}",
          "subdomain": subdomain,
          "domainId": self._domain_id(),
          "mode": "http",
        },
      )
      if not resource or "resourceId" not in resource:
        raise RouteProviderError(
          f"Pangolin did not return a resource id when creating {domain_name}"
        )
      # Nothing to clear on a resource that did not exist a moment ago.
      resource = {**resource, "targets": []}

    self._set_target(resource, port, scheme)

  def unregister_route(self, subdomain: str, domain: str):
    domain_name = self._domain_name(subdomain, domain)
    existing = self._find_resource(domain_name)
    if existing:
      logger.info("deleting Pangolin resource for %s", domain_name)
      self._request("DELETE", f"/public-resource/{existing['resourceId']}")

  def _harbor_resources(self) -> list[tuple[str, dict]]:
    """Yield (subdomain, resource) for single-label names under the harbor domain.

    Only ``<label>.<harbor_domain>`` counts -- multi-label names like
    ``qbt.arr.<domain>`` are other systems' routes and are ignored.
    """
    suffix = f".{self.harbor_domain}"
    out: list[tuple[str, dict]] = []
    for resource in self._resources():
      full_domain = resource.get("fullDomain") or ""
      if not full_domain.endswith(suffix):
        continue
      subdomain = full_domain[: -len(suffix)]
      if not subdomain or "." in subdomain:
        continue
      out.append((subdomain, resource))
    return out

  def list_routes(self) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for subdomain, resource in self._harbor_resources():
      targets = self._targets(resource)
      dest = (
        f"{targets[0].get('ip', '?')}:{targets[0].get('port', '?')}"
        if targets
        else "(no target)"
      )
      routes.append((subdomain, dest))
    return routes

  def route_owners(self) -> dict[str, str | None]:
    return {
      subdomain: self._owner(resource)
      for subdomain, resource in self._harbor_resources()
    }
