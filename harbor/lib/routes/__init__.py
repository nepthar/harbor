from __future__ import annotations

from typing import TYPE_CHECKING

from harbor.lib.config import NONE_ROUTE_PROVIDER_TAG

from .base import (
  NoopRouteProvider,
  RouteProvider,
  RouteProviderError,
  refuse_foreign_route,
)
from .npm import NginxProxyManagerRouteProvider
from .pangolin import PangolinRouteProvider

if TYPE_CHECKING:
  from harbor.lib.harbor import HarborCtx

__all__ = [
  "NginxProxyManagerRouteProvider",
  "NoopRouteProvider",
  "PangolinRouteProvider",
  "RouteProvider",
  "RouteProviderError",
  "get_route_provider",
  "refuse_foreign_route",
]

# Every provider harbor can build, keyed by the config `kind` that selects it.
# Adding a provider means adding it here and nowhere else in this module.
PROVIDERS: dict[str, type[RouteProvider]] = {
  cls.KIND: cls
  for cls in (
    NginxProxyManagerRouteProvider,
    PangolinRouteProvider,
    NoopRouteProvider,
  )
}


def get_route_provider(ctx: HarborCtx, tag: str) -> RouteProvider:
  """Build the route provider for ``tag``.

  A thin dispatcher: it resolves the tag to a config block and hands that block
  to the matching provider's ``from_config``. What each kind requires of its
  ``args`` is that provider's business, not this function's.
  """
  if tag == NONE_ROUTE_PROVIDER_TAG:
    return NoopRouteProvider(domain=ctx.config.provider_domain(tag))

  conf = ctx.config.route_providers.get(tag)
  if conf is None:
    known = ", ".join(sorted(ctx.config.route_providers))
    raise RouteProviderError(
      f"No route provider tagged {tag!r}; known tags: {known}. "
      f"Add [route_provider.{tag}] to config.toml or pick an existing tag"
    )

  provider = PROVIDERS.get(conf.kind)
  if provider is None:
    expected = ", ".join(repr(kind) for kind in sorted(PROVIDERS))
    raise RouteProviderError(
      f"route_provider.{tag}: unknown kind {conf.kind!r}; expected one of {expected}"
    )

  return provider.from_config(tag, conf, ctx)
