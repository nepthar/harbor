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

  ``args.shared_policy``, if set, is the niceId of a Pangolin shared policy
  (the slug in the policy's dashboard URL). Every route this instance
  registers is attached to that policy. Two ``[route_provider.*]`` blocks
  with different tags can point at different policies.
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
    shared_policy: str | None = None,
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
    self.shared_policy = shared_policy or None
    self._resolved_policy_id: int | None = None
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

  def _failure(
    self, method: str, path: str, resp: requests.Response, action: str
  ) -> str:
    """Explain a failed call, telling apart "API said no" from "not the API".

    The integration API is off by default on self-hosted Pangolin and runs on
    its own port, so `endpoint` very easily ends up pointing at the dashboard
    instead. That answers every path with the dashboard's own HTML 404, and
    echoing a page of markup back at the operator explains nothing.
    """
    try:
      body = resp.json()
    except ValueError:
      kind = resp.headers.get("Content-Type", "an unknown content type")
      return (
        f"Pangolin {method} {path} returned {resp.status_code} as {kind} rather "
        f"than JSON, so {self.endpoint} is not serving the integration API. "
        f"Point args.endpoint at the integration API's own host/port -- it is "
        f"separate from the dashboard, and needs enable_integration_api in "
        f"Pangolin's config.yml"
      )

    message = (body or {}).get("message") or resp.reason
    if resp.status_code == 403:
      # Pangolin's own 403 does not name the action it refused, and the key's
      # permissions are a checklist in the dashboard -- so name it here.
      #
      # Don't send the operator off to check org scoping: Pangolin runs its
      # org-access middleware *before* the per-action one, so a 403 that got
      # as far as the action check already proves the key is in the org. The
      # checklist is grouped by noun ("Resource", "Resource Policy", "Site"),
      # and the group is not always the one the endpoint's URL suggests.
      return (
        f"Pangolin refused {method} {path} ({resp.status_code}: {message}). The "
        f"API key is missing the {action!r} permission; enable it on the key "
        f"under the matching group in the dashboard's permission checklist"
      )
    if resp.status_code == 401:
      return (
        f"Pangolin rejected the API key ({resp.status_code}: {message}); check "
        f"that it is valid and scoped to org {self.org_id!r}"
      )
    return f"Pangolin {method} {path} returned {resp.status_code}: {message}"

  def _request(self, method: str, path: str, action: str, **kwargs):
    """Call the integration API and unwrap Pangolin's response envelope.

    Every response is ``{data, success, error, message, status}``; callers only
    ever want ``data``. ``action`` is the Pangolin permission the endpoint is
    guarded by, carried so a 403 can say which one the key is missing.
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

    if not resp.ok:
      raise RouteProviderError(self._failure(method, path, resp, action))

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
      data = (
        self._request("GET", f"/org/{self.org_id}/site/{self.site}", "getSite") or {}
      )
      site_id = data.get("siteId")
      if site_id is None:
        raise RouteProviderError(
          f"Pangolin org {self.org_id!r} has no site {self.site!r}; use the name "
          f"from the site's URL in the Pangolin dashboard"
        )
      self._resolved_site_id = int(site_id)
    return self._resolved_site_id

  # ── shared policies ───────────────────────────────────────────────────

  def _policy_id(self) -> int | None:
    """The numeric resourcePolicyId behind the configured niceId.

    Sites have a get-by-niceId on the integration API; shared policies do
    not, so we list and match. Create-resource also has no resourcePolicyId
    field -- the attach is a later update; see _apply_shared_policy.
    """
    if not self.shared_policy:
      return None
    if self._resolved_policy_id is None:
      data = (
        self._request(
          "GET",
          f"/org/{self.org_id}/public-resource-policies",
          "listResourcePolicies",
          params={"pageSize": 1000},
        )
        or {}
      )
      for policy in data.get("policies", []):
        if policy.get("niceId") == self.shared_policy:
          self._resolved_policy_id = int(policy["resourcePolicyId"])
          break
      else:
        raise RouteProviderError(
          f"Pangolin org {self.org_id!r} has no shared policy "
          f"{self.shared_policy!r}; use the name from the policy's URL in "
          f"the Pangolin dashboard"
        )
    return self._resolved_policy_id

  def _apply_shared_policy(self, resource: dict) -> None:
    """Attach the configured policy, unconditionally.

    There is no cheap "already attached?" check to short-circuit on: the list
    endpoint we get resources from does not return resourcePolicyId, so the
    only way to read the current one is a per-resource getResource call. That
    trades the idempotent write we would skip for a read of the same cost, so
    just do the write.
    """
    policy_id = self._policy_id()
    if policy_id is None:
      return
    rid = resource["resourceId"]
    self._request(
      "POST",
      f"/public-resource/{rid}",
      "updateResource",
      json={"resourcePolicyId": policy_id},
    )

  # ── domains ───────────────────────────────────────────────────────────

  def _domain(self) -> dict | None:
    """Return the org domain entry whose base domain is the harbor domain."""
    data = self._request("GET", f"/org/{self.org_id}/domains", "listOrgDomains") or {}
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
        "GET",
        f"/org/{self.org_id}/public-resources",
        "listResources",
        params={"pageSize": 1000},
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
    data = self._request("GET", f"/public-resource/{rid}/targets", "listTargets") or {}
    return data.get("targets", [])

  def _set_target(self, resource: dict, port: int, scheme: str) -> None:
    """Point the resource at ``harbor_address:port``, replacing any targets.

    Harbor owns the whole resource, so the target set is whatever we last
    wrote. Clearing and recreating keeps a re-registered route from stacking
    up stale targets that Pangolin would then load-balance across.
    """
    rid = resource["resourceId"]
    for target in self._targets(resource):
      self._request("DELETE", f"/target/{target['targetId']}", "deleteTarget")

    self._request(
      "PUT",
      f"/public-resource/{rid}/target",
      "createTarget",
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
      self._request("GET", f"/site/{self._site_id()}", "getSite")
    except RouteProviderError as e:
      errors.append(f"Pangolin site {self.site!r} is not usable: {e}")

    if self.shared_policy:
      try:
        self._policy_id()
      except RouteProviderError as e:
        errors.append(str(e))

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
    # Resolve first so a missing policy refuses before we create a resource.
    self._policy_id()
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
        "createResource",
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
    self._apply_shared_policy(resource)

  def unregister_route(self, subdomain: str, domain: str):
    domain_name = self._domain_name(subdomain, domain)
    existing = self._find_resource(domain_name)
    if existing:
      logger.info("deleting Pangolin resource for %s", domain_name)
      self._request(
        "DELETE", f"/public-resource/{existing['resourceId']}", "deleteResource"
      )

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
