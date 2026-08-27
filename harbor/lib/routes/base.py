from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from harbor.lib.apps import AppID
from harbor.lib.config import RouteProviderEntry
from harbor.lib.store import HarborStore

if TYPE_CHECKING:
  from harbor.lib.harbor import HarborCtx

logger = logging.getLogger("harbor.routes")


class RouteProviderError(Exception):
  """Raised when a route provider cannot complete an operation."""


def refuse_foreign_route(domain_name: str, owner: str | None) -> RouteProviderError:
  owner_desc = f"happ {owner!r}" if owner else "a non-Harbor proxy host"
  return RouteProviderError(
    f"Refusing to replace {domain_name}; it is already owned by {owner_desc}"
  )


class RouteProvider:
  # The config `kind` that selects this provider. `get_route_provider` builds
  # its dispatch table from these, so a new provider only has to set one.
  KIND: str = ""
  # `args` keys that must be present and non-empty in the provider's block.
  REQUIRED_ARGS: tuple[str, ...] = ()

  @classmethod
  def from_config(
    cls,
    tag: str,
    conf: RouteProviderEntry,
    ctx: HarborCtx,
  ) -> RouteProvider:
    """Build this provider from its ``[route_provider.<tag>]`` block."""
    raise NotImplementedError

  @classmethod
  def _args(cls, tag: str, conf: RouteProviderEntry) -> dict[str, str]:
    """A copy of the block's args, refusing if any REQUIRED_ARGS are missing."""
    missing = [name for name in cls.REQUIRED_ARGS if not conf.args.get(name)]
    if missing:
      raise RouteProviderError(
        f"route_provider.{tag}.args: missing {missing}; needs {cls.REQUIRED_ARGS}"
      )
    return dict(conf.args)

  @staticmethod
  def _secret(harbor_db: HarborStore, ref: str) -> str:
    value = harbor_db.get_secret(ref)
    if not value:
      raise RouteProviderError(
        f"Missing secret {ref!r}. Run: harbor config-sys --stdin {ref}"
      )
    return value

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
    """Map subdomain under the harbor domain -> owning harbor app id."""
    raise NotImplementedError

  def validate(self) -> list[str]:
    """Return an empty list if this is valid, or a list of errors"""
    raise NotImplementedError


class NoopRouteProvider(RouteProvider):
  """A route provider that records intent but does not configure any proxy."""

  KIND = "noop"

  @classmethod
  def from_config(
    cls,
    tag: str,
    conf: RouteProviderEntry,
    ctx: HarborCtx,
  ) -> NoopRouteProvider:
    return cls(domain=conf.domain)

  def __init__(self, domain: str = ""):
    self.domain = domain
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
    logger.warning(f"Noop route provider - {app} Registere {route}")

  def unregister_route(self, subdomain: str, domain: str):
    route = f"{subdomain}.{domain}"
    logger.debug(f"Noop route provider - Unregister {route}")
    self.routes.pop(subdomain, None)
    self.owners.pop(subdomain, None)

  def list_routes(self) -> list[tuple[str, str]]:
    return sorted(self.routes.items())

  def route_owners(self) -> dict[str, str | None]:
    return dict(self.owners)

  def validate(self) -> list[str]:
    return []
