"""`AppStack` plus per-installation data, out to a compose file.

`make_compose_dict` is the whole of harbor's compose generation. `AppRunData`
is a plain frozen dataclass, so these build one directly rather than going
through `load_run_data`, which needs a `HarborCtx` and a staged app -- that
path is covered end to end in test_cli.py. The readiness section at the bottom
covers the rest of what `AppRunData` decides.
"""

from __future__ import annotations

from pathlib import Path

from harbor.lib.run_layout import (
  AppRunData,
  AssignedRoute,
  ConfigIssue,
  ConfigValue,
  make_compose_dict,
)
from harbor.lib.stack import HARBOR_SUBDOMAIN_LABEL, AppConfig, AppStack
from tests.conftest import stack_of


def run_data(
  stack: AppStack,
  *,
  app_domain: str | None = None,
  host_ports: dict[str, int] | None = None,
  config_values: dict[str, ConfigValue] | None = None,
  issues: tuple[ConfigIssue, ...] = (),
) -> AppRunData:
  """An `AppRunData` for `stack`, with every route allocated.

  `host_ports` overrides the allocated host port per route name; anything not
  named keeps whatever the manifest pinned.
  """
  host_ports = host_ports or {}
  routes = {
    name: AssignedRoute(
      name=name,
      subdomain="",
      run_unit_name=route.run_unit_name,
      host_port=host_ports.get(name, route.host_port),
      container_port=route.container_port,
      proto=route.proto,
      publish=route.publish,
      scheme=route.scheme,
    )
    for name, route in stack.routes.items()
  }
  return AppRunData(
    app=stack.app,
    run_path=Path("/harbor/run") / stack.app,
    app_domain=app_domain,
    volume_links={},
    config_values=config_values or {},
    routes=routes,
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
  # The container gets the same mapping as an env var, so a happ's own scripts
  # can find their mounts without hardcoding paths.
  assert service["environment"]["HAPP_VOLUMES"] == "bin:/opt/bin,app_config:/config"


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
main  = { port = "8080", publish = "web" }
admin = { port = "9000:80" }
dns   = { port = "53/udp" }
""",
  )

  data = run_data(stack, host_ports={"main": 41000, "dns": 41001})
  service = make_compose_dict(stack, data)["services"]["main"]

  assert service["ports"] == ["41000:8080", "9000:80", "41001:53/udp"]
  assert service["environment"]["HAPP_ROUTES"] == "main:8080,admin:80,dns:53"


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
  assert service["environment"]["HAPP_CMD"] == "/bin/sh -c exec sleep 1"


def test_the_app_domain_reaches_both_env_and_labels(tmp_path):
  """The label is what `harbor doctor` and the route provider read back."""
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

  assert service["environment"]["HAPP_DOMAIN"] == "photos.harbor.localhost"
  assert service["labels"][HARBOR_SUBDOMAIN_LABEL] == "photos.harbor.localhost"


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
  the operator has to fix -- counting it made `harbor ps` report "needs config"
  for an app that needed none.
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
    issues=(),
  )

  assert data.config_env() == {
    "__HARBOR_CONFIG__admin_user": "alice",
    "__HARBOR_CONFIG__api_key": "",
  }
