"""Tests for the [run.<unit>.routes] model: port specs, private, and the
route-name -> subdomain mapping (reserved "main" = the bare app subdomain).
"""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock, patch

import pytest
import requests

from harbor.lib.apps import AppID
from harbor.lib.config import (
  NONE_ROUTE_PROVIDER_TAG,
  PLACEHOLDER_DOMAIN,
  Config,
  RouteProviderEntry,
  RouteProviderKind,
  load_config_file,
)
from harbor.lib.harbor import HarborCtx
from harbor.lib.lifecycle.routes import assigned_routes, preflight_app_routes
from harbor.lib.manifest import ConfigError, Manifest, _validate_routes
from harbor.lib.routes import (
  PROVIDERS,
  NginxProxyManagerRouteProvider,
  NoopRouteProvider,
  PangolinRouteProvider,
  RouteProvider,
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
main    = { port = "8080" }
api     = { port = "8081" }
admin   = { port = "8082", private = true }
default = { port = "8083", private = true }
metrics = { port = "9090:9091/udp", private = true }
"""


# ── resolution ────────────────────────────────────────────────────────────
def test_private_defaults_to_false():
  stack = _stack(ROUTES)
  assert stack.routes["main"].private is False
  assert stack.routes["api"].private is False


def test_private_true_resolved():
  stack = _stack(ROUTES)
  assert stack.routes["default"].private is True
  assert stack.routes["admin"].private is True


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
main  = { port = "8080" }
admin = { port = "8443:8443", scheme = "https" }
"""
  )
  assert stack.routes["main"].scheme == "http"
  assert stack.routes["admin"].scheme == "https"


def test_primary_route_subdomain_is_bare_app_subdomain():
  stack = _stack(ROUTES)
  main = stack.routes["main"]
  assert main.subdomain("photos") == "photos"
  assert main.private is False


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
main = { port = "8080" }

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
dash = { port = "8080" }

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
bad = { port = "8080", scheme = "ftp" }
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
main = { port = "8080" }
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
api = { port = "8080" }
"""
  )
  assert stack.run_units["main"].environment["PEER"] == "${routes.api}"


def test_env_reference_to_an_undeclared_route_is_rejected():
  with pytest.raises(ConfigError, match="not a known substitution"):
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


def test_env_reference_to_an_unknown_dotted_key_is_rejected():
  with pytest.raises(ConfigError, match="not a known substitution"):
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


def test_env_may_reference_happ_keys():
  stack = _stack(
    """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
env = { DOMAIN = "${happ.domain}", VOLS = "${happ.volumes}" }
"""
  )
  assert stack.run_units["main"].environment["DOMAIN"] == "${happ.domain}"
  assert stack.run_units["main"].environment["VOLS"] == "${happ.volumes}"


def test_env_reference_to_an_unknown_happ_key_is_rejected():
  with pytest.raises(ConfigError, match="not a known substitution"):
    _stack(
      """
[app]
version = "0.1.0"
subdomain = "photos"

[run.main]
image = "alpine:latest"
env = { HOST = "${happ.host}" }
"""
    )


def _config(tmp_path: Path, route_providers: dict) -> Config:
  return Config(
    config_path=tmp_path / "config.toml",
    harbor_root=tmp_path,
    volume_roots={},
    apps_root=tmp_path / "apps",
    run_root=tmp_path / "run",
    snapshot_root=tmp_path / "snapshots",
    master_key="",
    master_keyfile=tmp_path / "master.key",
    port_base=41000,
    harbor_address="192.168.1.10",
    default_route_provider="web",
    route_providers={
      NONE_ROUTE_PROVIDER_TAG: RouteProviderEntry(
        kind="noop", domain=PLACEHOLDER_DOMAIN
      ),
      **route_providers,
    },
  )


def _ctx(
  tmp_path: Path,
  route_providers: dict,
  *,
  secrets: dict[str, str] | None = None,
) -> HarborCtx:
  ctx = HarborCtx(_config(tmp_path, route_providers))
  for name, value in (secrets or {}).items():
    ctx.harbor_db.set_secret(name, value)
  return ctx


# ── provider dispatch ──────────────────────────────────────────────────────
def test_every_config_kind_has_a_provider():
  """The config schema and the dispatch table have to name the same kinds.

  Adding one to `RouteProviderKind` without registering a provider would parse
  fine and then refuse at start time, which is the wrong place to find out.
  """
  assert set(get_args(RouteProviderKind)) == set(PROVIDERS)


def test_provider_must_implement_from_config(tmp_path):
  class Halfway(RouteProvider):
    KIND = "halfway"

  with pytest.raises(NotImplementedError):
    Halfway.from_config(
      "web",
      RouteProviderEntry(kind="noop", domain="home.example"),
      HarborCtx(_config(tmp_path, {})),
    )


def test_required_args_are_checked_against_the_tagged_block():
  class Demanding(RouteProvider):
    KIND = "demanding"
    REQUIRED_ARGS = ("endpoint", "token")

  conf = RouteProviderEntry(
    kind="noop", domain="home.example", args={"endpoint": "http://x", "token": ""}
  )
  # An empty value is as missing as an absent key, and the error names the block.
  with pytest.raises(RouteProviderError, match=r"route_provider.web.args: missing"):
    Demanding._args("web", conf)

  conf.args["token"] = "t"
  args = Demanding._args("web", conf)
  assert args == {"endpoint": "http://x", "token": "t"}
  # A copy, so from_config can pop resolved args without touching the config.
  assert args is not conf.args


def _npm_provider():
  return NginxProxyManagerRouteProvider(
    endpoint="http://npm.example",
    email="admin@example.com",
    password="test-password",
    harbor_domain="home.example",
    harbor_address="192.168.1.10",
  )


def test_documented_route_provider_config_constructs(tmp_path):
  ctx = _ctx(
    tmp_path,
    {
      "web": RouteProviderEntry(
        kind="nginx_proxy_manager",
        domain="home.example",
        args={
          "endpoint": "http://npm.example",
          "email": "admin@example.com",
          "password_secret": "npm.password",
        },
      ),
    },
    secrets={"npm.password": "test-password"},
  )

  provider = get_route_provider(ctx, "web")

  assert isinstance(provider, NginxProxyManagerRouteProvider)
  assert provider.email == "admin@example.com"
  assert provider.harbor_address == "192.168.1.10"
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


# ── pangolin ───────────────────────────────────────────────────────────────
SITE = "substantial-atractaspis-branchi"


POLICY = "closed-spea-hammondii"


def _pangolin_provider(
  site: str = SITE, resolved: int | None = 3, shared_policy: str | None = None
):
  """A provider whose site slug is already resolved, unless `resolved` is None.

  Most tests are not about the site lookup, and leaving it unresolved would
  make every one of them stub that request too.
  """
  provider = PangolinRouteProvider(
    endpoint="https://pangolin.example:3003",
    api_key="test-key",
    org_id="acme",
    site=site,
    harbor_domain="home.example",
    harbor_address="192.168.1.10",
    shared_policy=shared_policy,
  )
  provider._resolved_site_id = resolved
  return provider


def _pangolin_ctx(tmp_path: Path, args: dict[str, str]):
  secrets = {}
  if secret := args.get("api_key_secret"):
    secrets[secret] = "test-key"
  return _ctx(
    tmp_path,
    {"web": RouteProviderEntry(kind="pangolin", domain="home.example", args=args)},
    secrets=secrets,
  )


PANGOLIN_ARGS = {
  "endpoint": "https://pangolin.example:3003",
  "org_id": "acme",
  "site": SITE,
  "api_key_secret": "pangolin.api_key",
}


def test_pangolin_config_constructs(tmp_path):
  provider = get_route_provider(_pangolin_ctx(tmp_path, PANGOLIN_ARGS), "web")

  assert isinstance(provider, PangolinRouteProvider)
  assert provider.org_id == "acme"
  assert provider.site == SITE
  assert provider.harbor_address == "192.168.1.10"
  assert provider.harbor_domain == "home.example"
  assert provider.shared_policy is None


def test_pangolin_config_passes_shared_policy(tmp_path):
  args = {**PANGOLIN_ARGS, "shared_policy": POLICY}
  provider = get_route_provider(_pangolin_ctx(tmp_path, args), "web")
  assert provider.shared_policy == POLICY


def test_pangolin_empty_shared_policy_is_unset():
  provider = _pangolin_provider(shared_policy="")
  assert provider.shared_policy is None


def test_pangolin_config_tags_can_carry_different_shared_policies(tmp_path):
  ctx = _pangolin_ctx(tmp_path, PANGOLIN_ARGS)
  ctx.config.route_providers["closed"] = RouteProviderEntry(
    kind="pangolin",
    domain="home.example",
    args={**PANGOLIN_ARGS, "shared_policy": POLICY},
  )
  ctx.config.route_providers["open"] = RouteProviderEntry(
    kind="pangolin",
    domain="open.example",
    args={**PANGOLIN_ARGS, "shared_policy": "open-policy-slug"},
  )

  closed = get_route_provider(ctx, "closed")
  opened = get_route_provider(ctx, "open")

  assert closed.shared_policy == POLICY
  assert opened.shared_policy == "open-policy-slug"


def _response(status: int, *, json_body=None, text: str = "", content_type: str = ""):
  resp = requests.Response()
  resp.status_code = status
  resp.headers["Content-Type"] = content_type
  resp._content = (json.dumps(json_body) if json_body is not None else text).encode()
  return resp


def test_pangolin_names_the_wrong_endpoint_on_an_html_error():
  """The dashboard answers /v1 with its own HTML 404, which says nothing."""
  provider = _pangolin_provider()
  resp = _response(
    404,
    text="<!DOCTYPE html><html lang='en-US'><head><title>404</title></head></html>",
    content_type="text/html; charset=utf-8",
  )

  message = provider._failure("GET", "/org/acme/domains", resp, "listOrgDomains")

  assert "not serving the integration API" in message
  assert "enable_integration_api" in message
  # The markup itself is not echoed back at the operator.
  assert "DOCTYPE" not in message


def test_pangolin_surfaces_the_api_message_on_a_json_error():
  provider = _pangolin_provider()
  resp = _response(
    404,
    json_body={"message": "Organization not found", "success": False},
    content_type="application/json",
  )

  message = provider._failure("GET", "/org/acme/domains", resp, "listOrgDomains")

  assert "Organization not found" in message
  assert "not serving the integration API" not in message


def test_pangolin_reports_a_rejected_key_with_its_reason():
  provider = _pangolin_provider()
  resp = _response(
    401, json_body={"message": "Unauthorized"}, content_type="application/json"
  )

  message = provider._failure("GET", "/org/acme/domains", resp, "listOrgDomains")

  assert "rejected the API key (401: Unauthorized)" in message
  assert "'acme'" in message


def test_pangolin_403_names_the_permission_the_key_is_missing():
  """Pangolin's 403 body never says which action it refused, so harbor must."""
  provider = _pangolin_provider()
  resp = _response(
    403,
    json_body={"message": "Key does not have permission perform this action"},
    content_type="application/json",
  )

  message = provider._failure(
    "PUT", "/org/acme/public-resource", resp, "createResource"
  )

  assert "'createResource' permission" in message
  assert "PUT /org/acme/public-resource" in message
  # Pangolin checks org access before the per-action permission, so reaching
  # this 403 already proves the key is scoped to the org -- don't send the
  # operator off to re-check that.
  assert "'acme'" not in message


def test_pangolin_resolves_the_site_name_once():
  """Config names the site the only way the dashboard shows it: its URL name."""
  provider = _pangolin_provider(resolved=None)
  provider._request = Mock(return_value={"siteId": 7, "name": "harbor host"})

  assert provider._site_id() == 7
  provider._request.assert_called_once_with("GET", f"/org/acme/site/{SITE}", "getSite")

  # Cached: a second route registration must not re-resolve it.
  assert provider._site_id() == 7
  assert provider._request.call_count == 1


def test_pangolin_refuses_an_unknown_site():
  provider = _pangolin_provider(site="no-such-site", resolved=None)
  provider._request = Mock(return_value={})

  with pytest.raises(RouteProviderError, match="has no site 'no-such-site'"):
    provider._site_id()


def test_pangolin_target_carries_the_resolved_site_id():
  provider = _pangolin_provider(resolved=None)
  provider._find_resource = Mock(return_value=None)
  provider._domain_id = Mock(return_value="dom_1")
  provider._site_id = Mock(return_value=7)
  provider._request = Mock(return_value={"resourceId": 12})

  provider.register_route(AppID("io.test.photos"), 41000, "photos", "home.example")

  target = provider._request.call_args_list[-1]
  assert target.kwargs["json"]["siteId"] == 7


def test_config_requires_harbor_address_for_proxying_providers(tmp_path):
  config = tmp_path / "config.toml"
  config.write_text(
    """
volume_root = "volumes"
default_route_provider = "web"

[route_provider.web]
kind = "pangolin"
domain = "home.example"
"""
  )
  with pytest.raises(ValueError, match="harbor_address"):
    load_config_file(config)


def test_config_allows_missing_harbor_address_when_only_noop(tmp_path):
  config = tmp_path / "config.toml"
  config.write_text(
    """
volume_root = "volumes"
default_route_provider = "web"

[route_provider.web]
kind = "noop"
domain = "home.example"
"""
  )
  assert load_config_file(config).harbor_address == ""


@pytest.mark.parametrize(
  "endpoint",
  [
    "http://pangolin.example:3003",
    "pangolin.example:3003",  # no scheme is not an implicit https
    "ftp://pangolin.example",
  ],
)
def test_pangolin_refuses_a_non_https_endpoint(endpoint):
  with pytest.raises(RouteProviderError, match="is not https"):
    PangolinRouteProvider(
      endpoint=endpoint,
      api_key="test-key",
      org_id="acme",
      site=SITE,
      harbor_domain="home.example",
      harbor_address="192.168.1.10",
    )


def test_pangolin_https_endpoint_keeps_case_and_drops_trailing_slash():
  provider = PangolinRouteProvider(
    endpoint="HTTPS://Pangolin.Example:3003/",
    api_key="test-key",
    org_id="acme",
    site=SITE,
    harbor_domain="home.example",
    harbor_address="192.168.1.10",
  )
  assert provider.endpoint == "HTTPS://Pangolin.Example:3003"


def test_pangolin_config_refuses_plaintext_endpoint(tmp_path):
  # The refusal has to survive the config path, not just direct construction.
  ctx = _pangolin_ctx(
    tmp_path, {**PANGOLIN_ARGS, "endpoint": "http://pangolin.example"}
  )
  with pytest.raises(RouteProviderError, match="is not https"):
    get_route_provider(ctx, "web")


def test_pangolin_config_does_not_touch_the_network(tmp_path):
  """Construction stays offline; the site lookup waits until a route needs it."""
  provider = get_route_provider(_pangolin_ctx(tmp_path, PANGOLIN_ARGS), "web")
  assert provider._resolved_site_id is None


def test_pangolin_config_reports_missing_args(tmp_path):
  args = {k: v for k, v in PANGOLIN_ARGS.items() if k != "org_id"}
  with pytest.raises(RouteProviderError, match="org_id"):
    get_route_provider(_pangolin_ctx(tmp_path, args), "web")


def test_pangolin_register_creates_resource_and_target():
  provider = _pangolin_provider()
  provider._find_resource = Mock(return_value=None)
  provider._domain_id = Mock(return_value="dom_1")
  provider._request = Mock(return_value={"resourceId": 12})
  app = AppID("io.test.photos")

  provider.register_route(app, 41000, "photos", "home.example")

  create, target = provider._request.call_args_list
  assert create.args == ("PUT", "/org/acme/public-resource", "createResource")
  assert create.kwargs["json"] == {
    "name": "harbor:io.test.photos",
    "subdomain": "photos",
    "domainId": "dom_1",
    "mode": "http",
  }
  assert target.args == ("PUT", "/public-resource/12/target", "createTarget")
  assert target.kwargs["json"] == {
    "siteId": 3,
    "ip": "192.168.1.10",
    "port": 41000,
    "method": "http",
    "mode": "http",
    "enabled": True,
  }


def test_pangolin_register_reuses_resource_and_replaces_targets():
  provider = _pangolin_provider()
  app = AppID("io.test.photos")
  provider._find_resource = Mock(
    return_value={
      "resourceId": 12,
      "name": f"harbor:{app}",
      "fullDomain": "photos.home.example",
      "targets": [{"targetId": 99}],
    }
  )
  provider._request = Mock()

  provider.register_route(app, 41001, "photos", "home.example")

  # No resource is created; the stale target is dropped before the new one.
  delete, create = provider._request.call_args_list
  assert delete.args == ("DELETE", "/target/99", "deleteTarget")
  assert create.args == ("PUT", "/public-resource/12/target", "createTarget")
  assert create.kwargs["json"]["port"] == 41001


def test_pangolin_register_refuses_foreign_owner():
  provider = _pangolin_provider()
  provider._request = Mock()
  provider._find_resource = Mock(
    return_value={
      "resourceId": 12,
      "name": "my hand-made resource",
      "fullDomain": "photos.home.example",
    }
  )

  with pytest.raises(RouteProviderError, match="already owned"):
    provider.register_route(AppID("io.test.photos"), 41000, "photos", "home.example")
  provider._request.assert_not_called()


def test_pangolin_register_forwards_https_scheme():
  provider = _pangolin_provider()
  provider._find_resource = Mock(return_value=None)
  provider._domain_id = Mock(return_value="dom_1")
  provider._request = Mock(return_value={"resourceId": 12})

  provider.register_route(
    AppID("io.test.photos"), 8443, "admin", "home.example", scheme="https"
  )

  target = provider._request.call_args_list[-1]
  assert target.kwargs["json"]["method"] == "https"
  assert target.kwargs["json"]["port"] == 8443


def test_pangolin_unregister_deletes_resource():
  provider = _pangolin_provider()
  provider._request = Mock()
  provider._find_resource = Mock(return_value={"resourceId": 12})

  provider.unregister_route("photos", "home.example")

  provider._request.assert_called_once_with(
    "DELETE", "/public-resource/12", "deleteResource"
  )


def test_pangolin_route_owners_maps_name_prefix():
  provider = _pangolin_provider()
  provider._resources = Mock(
    return_value=[
      {
        "resourceId": 1,
        "name": "harbor:io.test.photos",
        "fullDomain": "photos.home.example",
        "targets": [{"ip": "10.0.0.5", "port": 41000}],
      },
      {
        "resourceId": 2,
        "name": "manual",
        "fullDomain": "manual.home.example",
        "targets": [{"ip": "10.0.0.5", "port": 80}],
      },
      {
        "resourceId": 3,
        "name": "nested",
        "fullDomain": "qbt.arr.home.example",
        "targets": [],
      },
      {
        "resourceId": 4,
        "name": "harbor:ignored",
        "fullDomain": "other.example.com",
        "targets": [],
      },
      {"resourceId": 5, "name": "bare", "fullDomain": "home.example", "targets": []},
      {"resourceId": 6, "name": "tcp", "fullDomain": None, "targets": []},
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


def test_pangolin_validate_reports_missing_domain_and_site():
  provider = _pangolin_provider()
  provider._domain = Mock(return_value=None)
  provider._request = Mock(side_effect=RouteProviderError("404"))

  errors = provider.validate()

  assert any("has no domain 'home.example'" in e for e in errors)
  assert any(f"site {SITE!r} is not usable" in e for e in errors)


def test_pangolin_validate_passes_on_verified_domain():
  provider = _pangolin_provider()
  provider._domain = Mock(return_value={"domainId": "dom_1", "verified": True})
  provider._request = Mock(return_value={"siteId": 3})

  assert provider.validate() == []


def _policies(*nice_ids: str):
  return {
    "policies": [
      {"niceId": name, "resourcePolicyId": 40 + i} for i, name in enumerate(nice_ids)
    ]
  }


def test_pangolin_register_attaches_shared_policy_on_create():
  provider = _pangolin_provider(shared_policy=POLICY)
  provider._find_resource = Mock(return_value=None)
  provider._domain_id = Mock(return_value="dom_1")

  def _request(method, path, action, **kwargs):
    if action == "listResourcePolicies":
      return _policies(POLICY)
    return {"resourceId": 12}

  provider._request = Mock(side_effect=_request)

  provider.register_route(AppID("io.test.photos"), 41000, "photos", "home.example")

  attach = next(
    c for c in provider._request.call_args_list if c.args[2] == "updateResource"
  )
  assert attach.args == ("POST", "/public-resource/12", "updateResource")
  assert attach.kwargs["json"] == {"resourcePolicyId": 40}


def test_pangolin_register_attaches_shared_policy_on_reuse():
  provider = _pangolin_provider(shared_policy=POLICY)
  app = AppID("io.test.photos")
  provider._find_resource = Mock(
    return_value={
      "resourceId": 12,
      "name": f"harbor:{app}",
      "fullDomain": "photos.home.example",
      "targets": [{"targetId": 99}],
    }
  )

  def _request(method, path, action, **kwargs):
    if action == "listResourcePolicies":
      return _policies(POLICY)

  provider._request = Mock(side_effect=_request)

  provider.register_route(app, 41001, "photos", "home.example")

  attach = next(
    c for c in provider._request.call_args_list if c.args[2] == "updateResource"
  )
  assert attach.kwargs["json"] == {"resourcePolicyId": 40}


def test_pangolin_register_reattaches_policy_the_list_endpoint_omits():
  # Pangolin's list-resources endpoint does not return resourcePolicyId, so a
  # resource that already carries the policy is indistinguishable from one
  # that does not. Re-attaching is the idempotent way out; don't skip on a
  # field the real API never sends.
  provider = _pangolin_provider(shared_policy=POLICY)
  app = AppID("io.test.photos")
  provider._find_resource = Mock(
    return_value={
      "resourceId": 12,
      "name": f"harbor:{app}",
      "fullDomain": "photos.home.example",
      "targets": [{"targetId": 99}],
    }
  )

  def _request(method, path, action, **kwargs):
    if action == "listResourcePolicies":
      return _policies(POLICY)

  provider._request = Mock(side_effect=_request)

  provider.register_route(app, 41001, "photos", "home.example")

  attach = next(
    c for c in provider._request.call_args_list if c.args[2] == "updateResource"
  )
  assert attach.kwargs["json"] == {"resourcePolicyId": 40}


def test_pangolin_register_refuses_unknown_shared_policy():
  provider = _pangolin_provider(shared_policy=POLICY)
  provider._find_resource = Mock()
  provider._request = Mock(return_value={"policies": []})

  with pytest.raises(
    RouteProviderError, match="has no shared policy 'closed-spea-hammondii'"
  ):
    provider.register_route(AppID("io.test.photos"), 41000, "photos", "home.example")
  provider._find_resource.assert_not_called()


def test_pangolin_validate_reports_missing_shared_policy():
  provider = _pangolin_provider(shared_policy=POLICY)
  provider._domain = Mock(return_value={"domainId": "dom_1", "verified": True})

  def _request(method, path, action, **kwargs):
    if action == "listResourcePolicies":
      return {"policies": []}
    return {"siteId": 3}

  provider._request = Mock(side_effect=_request)

  errors = provider.validate()

  assert any("has no shared policy 'closed-spea-hammondii'" in e for e in errors)


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
  assignments = {
    name: "web" for name, route in stack.routes.items() if not route.private
  }
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
main = {{ port = "8080" }}
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
