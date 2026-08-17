"""`AppStack` plus per-installation data, out to a compose file.

`make_compose_dict` is the whole of harbor's compose generation. `AppRunData`
is a plain frozen dataclass, so these build one directly rather than going
through `load_run_data`, which needs a `HarborCtx` and a staged app -- that
path is covered end to end in test_cli.py. The readiness section at the bottom
covers the rest of what `AppRunData` decides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harbor.lib.apps import AppID
from harbor.lib.config import PLACEHOLDER_DOMAIN
from harbor.lib.manifest import ConfigError, parse_manifest
from harbor.lib.run_layout import (
  LOCALTIME_PATH,
  AppRunData,
  AssignedRoute,
  ConfigIssue,
  ConfigValue,
  _host_mounts,
  _route_urls,
  make_compose_dict,
)
from harbor.lib.stack import HARBOR_SUBDOMAIN_LABEL, AppConfig, AppStack
from tests.conftest import stack_of


def run_data(
  stack: AppStack,
  *,
  app_domain: str | None = None,
  domain: str = "home.example",
  host_ports: dict[str, int] | None = None,
  config_values: dict[str, ConfigValue] | None = None,
  host_mounts: tuple[str, ...] = (),
  issues: tuple[ConfigIssue, ...] = (),
  assignments: dict[str, str] | None = None,
) -> AppRunData:
  """An `AppRunData` for `stack`, with every route allocated.

  `host_ports` overrides the allocated host port per route name; anything not
  named keeps whatever the manifest pinned.
  """
  host_ports = host_ports or {}
  routes = {
    name: AssignedRoute(
      name=name,
      subdomain=route.subdomain(stack.subdomain) if stack.subdomain else "",
      run_unit_name=route.run_unit_name,
      host_port=host_ports.get(name, route.host_port),
      container_port=route.container_port,
      proto=route.proto,
      scheme=route.scheme,
    )
    for name, route in stack.routes.items()
  }
  if assignments is None:
    assignments = {
      name: "web" for name, route in stack.routes.items() if not route.private
    }
  config = type(
    "Cfg",
    (),
    {
      "provider_domain": lambda self, tag: (
        domain if tag and tag != "none" else PLACEHOLDER_DOMAIN
      )
    },
  )()
  return AppRunData(
    app=stack.app,
    run_path=Path("/harbor/run") / stack.app,
    app_domain=app_domain,
    volume_links={},
    config_values=config_values or {},
    routes=routes,
    route_urls=_route_urls(routes, assignments, config),
    host_mounts=host_mounts,
    issues=issues,
  )


# --- compose generation ----------------------------------------------------


def test_a_minimal_stack_becomes_a_minimal_compose_file(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1.2.3"

[run.main]
image = "alpine:latest"
""",
  )

  compose = make_compose_dict(stack, run_data(stack))

  assert compose == {
    "name": "demo",
    "services": {
      "main": {
        "image": "alpine:latest",
        "hostname": "main",
        "restart": "unless-stopped",
        "labels": {
          "harbor.app_id": "demo",
          "harbor.version": "1.2.3",
          "harbor.run_unit": "main",
        },
        "environment": {
          "HAPP_ID": "demo",
          "HAPP_VERSION": "1.2.3",
          "HAPP_RUN_UNIT": "main",
        },
      }
    },
  }


def test_the_project_name_is_the_app_id_made_compose_safe(tmp_path):
  """Compose project names are lowercase and take a narrow character set."""
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[run.main]
image = "alpine"
""",
    app_id="io.p2net.Basic-Features",
  )

  compose = make_compose_dict(stack, run_data(stack))

  assert compose["name"] == "io_p2net_basic-features"


def test_volumes_become_relative_bind_mounts(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[volumes]
bin        = { kind = "app" }
app_config = { kind = "data" }

[run.main]
image = "alpine"
volumes = { bin = "/opt/bin", app_config = "/config" }
""",
  )

  service = make_compose_dict(stack, run_data(stack))["services"]["main"]

  # Relative to the compose file, so the run dir stays movable. `app` volumes
  # are read-only, which is what `:ro` records.
  assert service["volumes"] == [
    "./volumes/app/bin:/opt/bin:ro",
    "./volumes/data/app_config:/config",
  ]
  assert "HAPP_VOLUMES" not in service["environment"]


def test_routes_become_published_ports(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
subdomain = "photos"

[run.main]
image = "alpine"

[run.main.routes]
main  = { port = "8080" }
admin = { port = "9000:80" }
dns   = { port = "53/udp" }
""",
  )

  data = run_data(stack, host_ports={"main": 41000, "dns": 41001})
  service = make_compose_dict(stack, data)["services"]["main"]

  assert service["ports"] == ["41000:8080", "9000:80", "41001:53/udp"]
  assert "HAPP_ROUTES" not in service["environment"]


def test_harbor_mounts_land_after_the_happs_own_and_outside_happ_volumes(tmp_path):
  """`${happ.volumes}` is the happ's own [volumes]; harbor mounts are not among them."""
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[volumes]
app_config = { kind = "data" }

[run.main]
image = "alpine"
volumes = { app_config = "/config" }
env = { VOLS = "${happ.volumes}" }
""",
  )

  data = run_data(stack, host_mounts=("/etc/localtime:/etc/localtime:ro",))
  service = make_compose_dict(stack, data)["services"]["main"]

  assert service["volumes"] == [
    "./volumes/data/app_config:/config",
    "/etc/localtime:/etc/localtime:ro",
  ]
  assert service["environment"]["VOLS"] == "app_config:/config"


def test_a_unit_with_no_volumes_of_its_own_still_gets_the_harbor_mounts(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[run.main]
image = "alpine"
""",
  )

  data = run_data(stack, host_mounts=("/etc/localtime:/etc/localtime:ro",))
  service = make_compose_dict(stack, data)["services"]["main"]

  assert service["volumes"] == ["/etc/localtime:/etc/localtime:ro"]
  assert "HAPP_VOLUMES" not in service["environment"]


def test_the_host_clock_is_mounted_read_only_when_the_host_has_one():
  """Images without tzdata cannot resolve a TZ name; the zone file is the fix."""
  if Path(LOCALTIME_PATH).exists():
    assert _host_mounts() == (f"{LOCALTIME_PATH}:{LOCALTIME_PATH}:ro",)
  else:
    # A mount docker cannot satisfy would fail every container at start.
    assert _host_mounts() == ()


def test_a_command_is_published_to_the_container_too(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[run.main]
image = "alpine"
cmd = ["/bin/sh", "-c", "exec sleep 1"]
""",
  )

  service = make_compose_dict(stack, run_data(stack))["services"]["main"]

  assert service["command"] == ["/bin/sh", "-c", "exec sleep 1"]
  assert "HAPP_CMD" not in service["environment"]


def test_compose_passthrough_lands_verbatim_in_the_service(tmp_path):
  """[run.<unit>.compose] is the escape hatch for anything harbor doesn't model."""
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[run.main]
image = "redis:7"

[run.main.compose.healthcheck]
test = "redis-cli ping || exit 1"
interval = "10s"

[run.side]
image = "alpine"
compose = { healthcheck = { disable = true } }
""",
  )

  services = make_compose_dict(stack, run_data(stack))["services"]

  assert services["main"]["healthcheck"] == {
    "test": "redis-cli ping || exit 1",
    "interval": "10s",
  }
  assert services["side"]["healthcheck"] == {"disable": True}


def test_compose_passthrough_may_not_shadow_harbor_managed_keys():
  manifest = b"""
[app]
version = "1"

[run.main]
image = "alpine"
compose = { image = "other", healthcheck = { disable = true } }
"""

  with pytest.raises(ConfigError, match="harbor manages these service keys"):
    parse_manifest(manifest, AppID("demo"), Path("manifest.toml"))


def test_the_app_domain_reaches_labels_not_env(tmp_path):
  """The label is what `harbor doctor` and the route provider read back.

  Apps that want the domain in env ask via `${happ.domain}`.
  """
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
subdomain = "photos"

[run.main]
image = "alpine"
""",
  )

  data = run_data(stack, app_domain="photos.harbor.localhost")
  service = make_compose_dict(stack, data)["services"]["main"]

  assert "HAPP_DOMAIN" not in service["environment"]
  assert service["labels"][HARBOR_SUBDOMAIN_LABEL] == "photos.harbor.localhost"


def test_a_route_reference_in_env_becomes_the_published_url(tmp_path):
  """`${routes.<name>}` is the app telling itself where it answers.

  The URL is not knowable when the stack is built -- it needs the harbor
  domain and an allocated route -- so it survives as a placeholder until the
  compose file is written.
  """
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
subdomain = "mealie"

[run.main]
image = "alpine"
env = { BASE_URL = "${routes.main}", API = "${routes.api}/v1" }

[run.main.routes]
main = { port = "9000" }
api  = { port = "9001" }
""",
  )

  env = make_compose_dict(stack, run_data(stack))["services"]["main"]["environment"]

  # Assigned non-private routes use the provider domain. Public URLs are
  # https (TLS terminates at the reverse proxy); `scheme` on a route is only
  # how the proxy dials the backend.
  # "main" is the one route that gets the bare app subdomain.
  assert env["BASE_URL"] == "https://mealie.home.example"
  assert env["API"] == "https://api-mealie.home.example/v1"


def test_a_route_reference_survives_alongside_a_config_reference(tmp_path):
  """One flat map, two value kinds: config becomes a compose env var name."""
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
subdomain = "mealie"

[config]
timezone = { default = "UTC" }

[run.main]
image = "alpine"
env = { GREETING = "${timezone} at ${routes.main}" }

[run.main.routes]
main = { port = "9000" }
""",
  )

  assert stack.run_units["main"].environment["GREETING"] == (
    "${timezone} at ${routes.main}"
  )

  env = make_compose_dict(stack, run_data(stack))["services"]["main"]["environment"]

  assert env["GREETING"] == (
    "${__HARBOR_CONFIG__timezone} at https://mealie.home.example"
  )


def test_happ_references_in_env_become_runtime_context(tmp_path):
  """`${happ.x}` is the app asking for what harbor used to inject as HAPP_*."""
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
subdomain = "jrnl"

[volumes]
data = { kind = "data" }

[run.main]
image = "alpine"
cmd = ["/bin/sh", "-c", "exec sleep 1"]
volumes = { data = "/data" }

[run.main.env]
DOMAIN = "${happ.domain}"
VOLS = "${happ.volumes}"
CMD = "${happ.cmd}"
ROUTES = "${happ.routes}"

[run.main.routes]
main = { port = "8080" }
""",
  )

  data = run_data(stack, app_domain="jrnl.home.example", host_ports={"main": 41000})
  env = make_compose_dict(stack, data)["services"]["main"]["environment"]

  assert env["DOMAIN"] == "jrnl.home.example"
  assert env["VOLS"] == "data:/data"
  assert env["CMD"] == "/bin/sh -c exec sleep 1"
  assert env["ROUTES"] == "main:8080"
  assert "HAPP_DOMAIN" not in env
  assert "HAPP_VOLUMES" not in env
  assert "HAPP_CMD" not in env
  assert "HAPP_ROUTES" not in env


def test_host_network_mode_is_set_per_service(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
network_mode = "host"

[run.main]
image = "alpine"
""",
  )

  service = make_compose_dict(stack, run_data(stack))["services"]["main"]

  assert service["network_mode"] == "host"


def test_every_run_unit_gets_its_own_service(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "2"
main = "web"

[run.web]
image = "nginx:1.27"

[run.db]
image = "postgres:16"
restart = "always"
""",
  )

  services = make_compose_dict(stack, run_data(stack))["services"]

  assert set(services) == {"web", "db"}
  assert services["db"]["restart"] == "always"
  assert services["web"]["restart"] == "unless-stopped"
  assert services["db"]["hostname"] == "db"


# --- readiness -------------------------------------------------------------


def test_start_blockers_leave_out_what_staging_repairs_itself():
  """`stage()` reallocates every route before judging readiness.

  An unallocated route is therefore the normal pre-start state, not something
  the operator has to fix -- counting it made `harbor ps` report CONFIG as
  missing for an app that needed none.
  """
  operator = ConfigIssue("config api_key is unset", "Set with `harbor config`")
  allocation = ConfigIssue("route web: not allocated", "…", self_healing=True)
  fatal = ConfigIssue("volume data: unreadable", "…", stage_blocking=True)

  data = AppRunData(
    app="demo",
    run_path=Path("/harbor/run/demo"),
    app_domain=None,
    volume_links={},
    config_values={},
    routes={},
    route_urls={},
    host_mounts=(),
    issues=(operator, allocation, fatal),
  )

  assert data.start_blockers == (operator, fatal)
  assert data.stage_blockers == (fatal,)


def test_config_env_names_every_value_including_the_unset_ones():
  """compose interpolates every `${__HARBOR_CONFIG__*}` the stack mentions.

  A missing key would make compose warn and render the variable blank, so an
  unset value has to appear as an empty string rather than not at all.
  """
  data = AppRunData(
    app="demo",
    run_path=Path("/harbor/run/demo"),
    app_domain=None,
    volume_links={},
    config_values={
      "admin_user": ConfigValue(AppConfig("admin_user", False, None, None), "alice"),
      "api_key": ConfigValue(AppConfig("api_key", True, None, None), None),
    },
    routes={},
    route_urls={},
    host_mounts=(),
    issues=(),
  )

  assert data.config_env() == {
    "__HARBOR_CONFIG__admin_user": "alice",
    "__HARBOR_CONFIG__api_key": "",
  }
