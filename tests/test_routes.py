"""Tests for the [run.<unit>.routes] model: port specs, public, and the
route-name -> subdomain mapping (reserved "main" = the bare app subdomain).
"""

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from harbor.lib.apps import AppID
from harbor.lib.config import NONE_ROUTE_PROVIDER_TAG, PLACEHOLDER_DOMAIN, Config
from harbor.lib.lifecycle.routes import assigned_routes, preflight_app_routes
from harbor.lib.manifest import ConfigError, Manifest, _validate_routes
from harbor.lib.routes import (
  NginxProxyManagerRouteProvider,
  NoopRouteProvider,
  RouteProviderError,
  get_route_provider,
)
from harbor.lib.run_layout import AppRunData, AssignedRoute, _route_urls
from harbor.lib.stack import AppStack


def _model(body: str) -> Manifest:
  """Schema-parsed only: what the `_validate_*` checks take as input."""
  return Manifest.model_validate(tomllib.loads(body))


def _stack(body: str, app_id: str = "io.test.example"):
  return AppStack.from_bytes(body.encode(), AppID(app_id), Path("manifest.toml"))


ROUTES = """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"

[run.main.routes]
main    = { port = "8080", public = true }
api     = { port = "8081", public = true }
admin   = { port = "8082" }
default = { port = "8083" }
metrics = { port = "9090:9091/udp" }
"""


# ── resolution ────────────────────────────────────────────────────────────
def test_public_defaults_to_false():
  stack = _stack(ROUTES)
  assert stack.routes["default"].public is False
  assert stack.routes["admin"].public is False


def test_public_true_resolved():
  stack = _stack(ROUTES)
  assert stack.routes["main"].public is True
  assert stack.routes["api"].public is True


def test_scheme_defaults_to_http():
  stack = _stack(ROUTES)
  assert stack.routes["main"].scheme == "http"
  assert stack.routes["admin"].scheme == "http"


def test_scheme_https_resolved():
  stack = _stack(
    """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"

[run.main.routes]
main  = { port = "8080", public = true }
admin = { port = "8443:8443", public = true, scheme = "https" }
"""
  )
  assert stack.routes["main"].scheme == "http"
  assert stack.routes["admin"].scheme == "https"


def test_primary_route_subdomain_is_bare_app_subdomain():
  stack = _stack(ROUTES)
  main = stack.routes["main"]
  assert main.subdomain("photos") == "photos"
  assert main.public is True


def test_named_route_subdomain_is_prefixed():
  stack = _stack(ROUTES)
  assert stack.routes["api"].subdomain("photos") == "api-photos"
  assert stack.routes["admin"].subdomain("photos") == "admin-photos"


def test_port_spec_parsed_onto_route():
  stack = _stack(ROUTES)
  # bare "8080" -> host auto-assigned, container 8080
  main = stack.routes["main"]
  assert (main.host_port, main.container_port, main.proto) == (-1, 8080, "tcp")
  assert main.needs_allocation is True
  # "9090:9091/udp" -> host 9090, container 9091, udp (no allocation)
  metrics = stack.routes["metrics"]
  assert (metrics.host_port, metrics.container_port, metrics.proto) == (
    9090,
    9091,
    "udp",
  )
  assert metrics.needs_allocation is False


def test_routes_report_their_run_unit():
  stack = _stack(ROUTES)
  assert {r.run_unit_name for r in stack.routes.values()} == {"main"}
  # the resolved port also lands on the run unit's port map
  assert set(stack.run_units["main"].routes) == {
    "main",
    "api",
    "admin",
    "default",
    "metrics",
  }


def test_routes_across_multiple_run_units():
  stack = _stack(
    """
[app]
version = "0.1.0"
subdomain = "photos"
main = "web"

[run.web]
image = "alpine:latest"
[run.web.routes]
main = { port = "8080", public = true }

[run.worker]
image = "alpine:latest"
[run.worker.routes]
metrics = { port = "9090" }
"""
  )
  assert stack.routes["main"].run_unit_name == "web"
  assert stack.routes["metrics"].run_unit_name == "worker"


# ── assigned-route filtering (lifecycle) ───────────────────────────────────
def test_assigned_routes_skips_none_and_unassigned():
  stack = _stack(ROUTES)
  run_data = _run_data(stack)
  store = SimpleNamespace(
    list_route_assignments=lambda: {"main": "web", "api": NONE_ROUTE_PROVIDER_TAG}
  )
  ctx = SimpleNamespace(app_store=lambda _app: store)
  names = {name for name, _, _ in assigned_routes(run_data, ctx)}
  assert names == {"main"}


# ── validation ────────────────────────────────────────────────────────────
def test_any_route_requires_app_subdomain():
  errors = _validate_routes(
    _model(
      """
[app]
version = "0.1.0"

[run.main]
image = "alpine:latest"
[run.main.routes]
admin = { port = "8082" }
"""
    )
  )
  assert any("subdomain" in e for e in errors)


def test_duplicate_route_name_across_units_is_rejected():
  errors = _validate_routes(
    _model(
      """
[app]
version = "0.1.0"
subdomain = "photos"

[run.a]
image = "alpine:latest"
[run.a.routes]
dash = { port = "8080" }

[run.b]
image = "alpine:latest"
[run.b.routes]
dash = { port = "8081" }
"""
    )
  )
  assert any("dash" in e and "multiple run units" in e for e in errors)


def test_duplicate_route_name_never_reaches_a_stack():
  """The app-level uniqueness rule, from the entry point rather than the check.

  `_build` flattens per-unit routes into one app-level mapping, so a duplicate
  that got past validation would not raise -- the last unit declared would
  quietly win, and the other unit's port would vanish from the compose file.
  """
  with pytest.raises(ConfigError, match="multiple run units"):
    _stack(
      """
[app]
version = "0.1.0"
subdomain = "photos"
main = "web"

[run.web]
image = "alpine:latest"
[run.web.routes]
dash = { port = "8080", public = true }

[run.worker]
image = "alpine:latest"
[run.worker.routes]
dash = { port = "8081" }
"""
    )


def test_host_network_mode_forbids_routes():
  errors = _validate_routes(
    _model(
      """
[app]
version = "0.1.0"
network_mode = "host"
subdomain = "photos"

[run.main]
image = "alpine:latest"
[run.main.routes]
admin = { port = "8082" }
"""
    )
  )
  assert any("host" in e for e in errors)


# ── schema ────────────────────────────────────────────────────────────────
def test_invalid_port_spec_rejected():
  with pytest.raises(ConfigError, match="is not a port number"):
    _stack(
      """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
[run.main.routes]
bad = { port = "not-a-port" }
"""
    )


def test_unknown_scheme_value_rejected():
  with pytest.raises(ConfigError):
    _stack(
      """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
[run.main.routes]
bad = { port = "8080", public = true, scheme = "ftp" }
"""
    )


def test_removed_top_level_routes_section_rejected():
  # [routes] is gone; it must no longer be accepted at the top level.
  with pytest.raises(ConfigError):
    _stack(
      """
[app]
version = "0.1.0"

[routes]
site = { audience = "web" }
"""
    )


# ── ${routes.<name>} references in [run.*.env] ────────────────────────────
def test_env_may_reference_any_declared_route():
  # Resolving it needs an allocated route; see test_compose.py for the value.
  stack = _stack(
    """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
env = { BASE_URL = "${routes.main}", ADMIN = "${routes.admin}" }
[run.main.routes]
main = { port = "8080", public = true }
admin = { port = "8082" }
"""
  )
  assert stack.run_units["main"].environment["BASE_URL"] == "${routes.main}"
  assert stack.run_units["main"].environment["ADMIN"] == "${routes.admin}"


def test_env_may_reference_a_route_on_another_run_unit():
  """Routes are app-level, so a unit can name one it does not itself publish."""
  stack = _stack(
    """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
env = { PEER = "${routes.api}" }

[run.api]
image = "alpine:latest"
[run.api.routes]
api = { port = "8080", public = true }
"""
  )
  assert stack.run_units["main"].environment["PEER"] == "${routes.api}"


def test_env_reference_to_an_undeclared_route_is_rejected():
  with pytest.raises(ConfigError, match="not declared in"):
    _stack(
      """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
env = { BASE_URL = "${routes.nope}" }
"""
    )


def test_env_reference_to_an_unknown_namespace_is_rejected():
  with pytest.raises(ConfigError, match="not a known namespace"):
    _stack(
      """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
env = { BASE_URL = "${volumes.data}" }
"""
    )


class _RouteDB:
  def get_secret(self, name):
    return "test-password"

  def get_token(self, name):
    return None, None


def _npm_provider():
  return NginxProxyManagerRouteProvider(
    endpoint="http://npm.example",
    email="admin@example.com",
    password="test-password",
    harbor_domain="home.example",
    forward_host="192.168.1.10",
  )


def test_documented_route_provider_config_constructs():
  config = Config(
    harbor_root=Path("/tmp"),
    volume_roots={},
    apps_root=Path("/tmp/apps"),
    run_root=Path("/tmp/run"),
    snapshot_root=Path("/tmp/snapshots"),
    master_key="",
    master_keyfile=Path("/tmp/master.key"),
    default_route_provider="web",
    route_providers={
      NONE_ROUTE_PROVIDER_TAG: {"kind": "noop", "domain": PLACEHOLDER_DOMAIN},
      "web": {
        "kind": "nginx_proxy_manager",
        "domain": "home.example",
        "endpoint": "http://npm.example",
        "email": "admin@example.com",
        "password_secret": "npm.password",
        "forward_host": "192.168.1.10",
      },
    },
  )

  provider = get_route_provider(_RouteDB(), config, "web")

  assert isinstance(provider, NginxProxyManagerRouteProvider)
  assert provider.email == "admin@example.com"
  assert provider.forward_host == "192.168.1.10"
  assert provider.harbor_domain == "home.example"


def test_register_route_requires_wildcard_certificate():
  provider = _npm_provider()
  provider._wildcard_certificate_id = Mock(return_value=None)

  with pytest.raises(RouteProviderError, match="wildcard certificate"):
    provider.register_route(AppID("io.test.photos"), 41000, "photos", "home.example")


def test_register_route_creates_updates_and_refuses_foreign_owner():
  provider = _npm_provider()
  provider._wildcard_certificate_id = Mock(return_value=7)
  provider._request = Mock()
  provider._find_proxy_host = Mock(return_value=None)
  app = AppID("io.test.photos")

  provider.register_route(app, 41000, "photos", "home.example")

  method, path = provider._request.call_args.args
  payload = provider._request.call_args.kwargs["json"]
  assert (method, path) == ("POST", "/api/nginx/proxy-hosts")
  assert payload["domain_names"] == ["photos.home.example"]
  assert payload["forward_scheme"] == "http"
  assert payload["certificate_id"] == 7
  assert payload["ssl_forced"] is True
  assert payload["meta"]["harbor_app"] == app

  provider._request.reset_mock()
  provider._find_proxy_host.return_value = {
    "id": 42,
    "meta": {"harbor_app": app},
  }
  provider.register_route(app, 41001, "photos", "home.example")
  assert provider._request.call_args.args == (
    "PUT",
    "/api/nginx/proxy-hosts/42",
  )

  provider._find_proxy_host.return_value = {
    "id": 43,
    "meta": {"harbor_app": "io.test.other"},
  }
  with pytest.raises(RouteProviderError, match="already owned"):
    provider.register_route(app, 41002, "photos", "home.example")


def test_register_route_forwards_https_scheme():
  provider = _npm_provider()
  provider._wildcard_certificate_id = Mock(return_value=7)
  provider._request = Mock()
  provider._find_proxy_host = Mock(return_value=None)

  provider.register_route(
    AppID("io.test.photos"), 8443, "admin", "home.example", scheme="https"
  )

  payload = provider._request.call_args.kwargs["json"]
  assert payload["forward_scheme"] == "https"
  assert payload["forward_port"] == 8443


def test_unregister_route_deletes_existing_proxy_host():
  provider = _npm_provider()
  provider._request = Mock()
  provider._find_proxy_host = Mock(return_value={"id": 42})

  provider.unregister_route("photos", "home.example")

  provider._request.assert_called_once_with("DELETE", "/api/nginx/proxy-hosts/42")


def test_npm_route_owners_maps_harbor_meta():
  provider = _npm_provider()
  provider._request = Mock(
    return_value=[
      {
        "domain_names": ["photos.home.example"],
        "forward_host": "10.0.0.5",
        "forward_port": 41000,
        "meta": {"harbor_app": "io.test.photos"},
      },
      {
        "domain_names": ["manual.home.example"],
        "forward_host": "10.0.0.5",
        "forward_port": 80,
        "meta": {},
      },
      {
        "domain_names": ["qbt.arr.home.example"],
        "forward_host": "10.0.0.20",
        "forward_port": 9405,
        "meta": {},
      },
      {
        "domain_names": ["other.example.com"],
        "forward_host": "1.2.3.4",
        "forward_port": 443,
        "meta": {"harbor_app": "ignored"},
      },
      {
        "domain_names": ["home.example"],
        "forward_host": "10.0.0.1",
        "forward_port": 80,
        "meta": {},
      },
    ]
  )

  assert provider.route_owners() == {
    "photos": "io.test.photos",
    "manual": None,
  }
  assert provider.list_routes() == [
    ("photos", "10.0.0.5:41000"),
    ("manual", "10.0.0.5:80"),
  ]


# ── preflight against the route provider ───────────────────────────────────
def _assigned(stack, route_name: str, host_port: int = 41000) -> AssignedRoute:
  route = stack.routes[route_name]
  assert stack.subdomain is not None
  return AssignedRoute(
    name=route_name,
    subdomain=route.subdomain(stack.subdomain),
    run_unit_name=route.run_unit_name,
    host_port=host_port,
    container_port=route.container_port,
    proto=route.proto,
    scheme=route.scheme,
  )


def _run_data(stack, host_ports: dict[str, int] | None = None) -> AppRunData:
  ports = host_ports or {}
  assigned = {
    name: _assigned(stack, name, ports.get(name, 41000 + i))
    for i, name in enumerate(stack.routes)
  }
  config = SimpleNamespace(
    provider_domain=lambda tag: (
      "home.example" if tag and tag != "none" else PLACEHOLDER_DOMAIN
    )
  )
  assignments = {name: "web" for name, route in stack.routes.items() if route.public}
  return AppRunData(
    app=stack.app,
    run_path=Path("/tmp/unused"),
    app_domain=f"{stack.subdomain}.home.example" if stack.subdomain else None,
    volume_links={},
    config_values={},
    routes=assigned,
    route_urls=_route_urls(assigned, assignments, config),
    host_mounts=(),
    issues=(),
  )


def _web_stack(app_id: str, subdomain: str):
  return _stack(
    f"""
[app]
version = "0.1.0"
subdomain = "{subdomain}"

[run.main]
image = "alpine:latest"
[run.main.routes]
main = {{ port = "8080", public = true }}
""",
    app_id,
  )


def _preflight_with(provider, stack):
  store = SimpleNamespace(
    list_route_assignments=lambda: {"main": "web"},
  )
  ctx = SimpleNamespace(
    config=SimpleNamespace(
      provider_domain=lambda tag: "home.example",
      route_providers={"web": {"kind": "noop", "domain": "home.example"}},
    ),
    harbor_db=lambda: None,
    app_store=lambda _app: store,
  )
  with patch("harbor.lib.lifecycle.routes.get_route_provider", return_value=provider):
    preflight_app_routes(_run_data(stack), ctx)


def test_preflight_allows_free_and_own_routes():
  provider = NoopRouteProvider()
  provider.register_route(AppID("flame"), 41000, "flame", "home.example")
  _preflight_with(provider, _web_stack("flame", "flame"))
  _preflight_with(NoopRouteProvider(), _web_stack("fresh", "fresh"))


def test_preflight_refuses_foreign_owner():
  provider = NoopRouteProvider()
  provider.register_route(AppID("first"), 41000, "shared", "home.example")
  with pytest.raises(RouteProviderError, match="already owned by happ 'first'"):
    _preflight_with(provider, _web_stack("second", "shared"))


def test_noop_first_publisher_wins():
  provider = NoopRouteProvider()
  provider.register_route(AppID("first"), 41000, "photos", "home.example")
  with pytest.raises(RouteProviderError, match="already owned"):
    provider.register_route(AppID("second"), 41001, "photos", "home.example")
  provider.register_route(AppID("first"), 41002, "photos", "home.example")
  assert provider.routes["photos"] == ":41002"
