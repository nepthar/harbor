"""Manifest bytes in, `AppStack` out.

`app_stack` is the whole path: parse the TOML, validate it against the app id,
resolve it into an installation-independent definition. What a manifest *means*
-- which defaults appear, how config interpolates into env, how a port string
becomes a route -- was previously only observable through generated compose
files in the CLI tests.
"""

from __future__ import annotations

from harbor.lib.stack import (
  HARBOR_APP_ID_LABEL,
  HARBOR_RUN_UNIT_LABEL,
  HARBOR_VERSION_LABEL,
)
from tests.conftest import stack_of


def test_a_minimal_manifest_fills_in_every_default(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1.2.3"

[run.main]
image = "alpine:latest"
""",
  )

  assert stack.app == "demo"
  assert stack.network_mode == "normal"
  assert stack.subdomain is None
  assert stack.routes == {}
  assert stack.config == {}
  assert stack.volumes == {}

  main = stack.run_units["main"]
  assert list(stack.run_units) == ["main"]
  assert main.image == "alpine:latest"
  assert main.hostname == "main"
  assert main.command is None
  assert main.restart == "unless-stopped"
  assert main.volumes == {}
  assert main.routes == {}

  # Every unit gets its identity in both env and labels, with no manifest
  # having asked for it.
  assert main.environment == {
    "HAPP_ID": "demo",
    "HAPP_VERSION": "1.2.3",
    "HAPP_RUN_UNIT": "main",
  }
  assert main.labels == {
    HARBOR_APP_ID_LABEL: "demo",
    HARBOR_VERSION_LABEL: "1.2.3",
    HARBOR_RUN_UNIT_LABEL: "main",
  }


def test_config_becomes_env_placeholders_not_values(tmp_path):
  """A stack is installation-independent, so config resolves to a variable name.

  The value is substituted by compose at run time from `AppRunData.config_env`.
  """
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[config]
admin_user = { desc = "who administers this" }
admin_pass = { secret = true }
mongo_pass = { secret = true, default = "auto" }
port = { default = "8080" }

[run.main]
image = "alpine"
env = { USER = "${admin_user}", PASS = "${admin_pass}", PORT = "${port}", PLAIN = "literal", UNKNOWN = "${nope}" }
""",
  )

  admin_user = stack.config["admin_user"]
  assert admin_user.desc == "who administers this"
  assert admin_user.secret is False
  assert admin_user.default is None
  assert admin_user.has_default() is False
  assert admin_user.env_name() == "__HARBOR_CONFIG__admin_user"

  assert stack.config["port"].has_default() is True
  assert stack.config["admin_pass"].secret is True
  # A secret with a default is still generated per installation, never taken
  # from the manifest -- so it does not count as having one.
  assert stack.config["mongo_pass"].default == "auto"
  assert stack.config["mongo_pass"].has_default() is False

  env = stack.run_units["main"].environment
  assert env["USER"] == "${__HARBOR_CONFIG__admin_user}"
  assert env["PASS"] == "${__HARBOR_CONFIG__admin_pass}"
  assert env["PORT"] == "${__HARBOR_CONFIG__port}"
  assert env["PLAIN"] == "literal"
  # Not a declared config name, so it is left for compose to deal with.
  assert env["UNKNOWN"] == "${nope}"


def test_volumes_resolve_by_kind_and_app_volumes_are_forced_readonly(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"

[volumes]
bin        = { kind = "app", src = "scripts" }
app_config = { kind = "data" }
cache      = { kind = "temp" }
media      = { kind = "bulk", readonly = true }
hostvol    = { kind = "host" }

[run.main]
image = "alpine"
volumes = { bin = "/opt/bin", app_config = "/config", media = "/media" }
""",
  )

  # `app` volumes carry the happ's own files, so harbor mounts them read-only
  # whether or not the manifest said so.
  assert stack.volumes["bin"].readonly is True
  assert stack.volumes["bin"].src == "scripts"
  assert stack.volumes["bin"].run_rel_path == "./volumes/app/bin"

  assert stack.volumes["app_config"].readonly is False
  assert stack.volumes["app_config"].run_rel_path == "./volumes/data/app_config"
  assert stack.volumes["cache"].run_rel_path == "./volumes/temp/cache"
  assert stack.volumes["media"].readonly is True
  assert stack.volumes["hostvol"].run_rel_path == "./volumes/host/hostvol"

  # Declared but unmounted volumes still belong to the stack -- staging links
  # them regardless of whether a run unit asked for one.
  assert set(stack.volumes) == {"bin", "app_config", "cache", "media", "hostvol"}

  mounts = stack.run_units["main"].volumes
  assert set(mounts) == {"bin", "app_config", "media"}
  assert mounts["app_config"].guest_path == "/config"
  assert mounts["app_config"].readonly is False
  assert mounts["bin"].guest_path == "/opt/bin"
  assert mounts["bin"].readonly is True


def test_port_strings_become_routes(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "1"
subdomain = "photos"

[run.main]
image = "alpine"

[run.main.routes]
main   = { port = "8080" }
admin  = { port = "9000:80" }
dns    = { port = "53/udp" }
secure = { port = "8443", scheme = "https" }
""",
  )

  # No host side, so harbor allocates one at start.
  primary = stack.routes["main"]
  assert primary.host_port == -1
  assert primary.needs_allocation is True
  assert primary.container_port == 8080
  assert primary.proto == "tcp"
  assert primary.private is False
  assert primary.scheme == "http"
  assert primary.run_unit_name == "main"

  # Pinned host port.
  admin = stack.routes["admin"]
  assert (admin.host_port, admin.container_port) == (9000, 80)
  assert admin.needs_allocation is False
  assert admin.private is False

  assert stack.routes["dns"].container_port == 53
  assert stack.routes["dns"].proto == "udp"
  assert stack.routes["secure"].scheme == "https"

  # The reserved name "main" takes the bare app subdomain; everything else is
  # labelled by route name.
  assert primary.subdomain("photos") == "photos"
  assert stack.routes["secure"].subdomain("photos") == "secure-photos"


def test_multiple_run_units_share_one_route_namespace(tmp_path):
  stack = stack_of(
    tmp_path,
    """\
[app]
version = "2"
main = "web"
subdomain = "demo"

[run.web]
image = "nginx:1.27"

[run.web.routes]
main = { port = "80" }

[run.db]
image = "postgres:16"
cmd = ["postgres", "-c", "max_connections=50"]
restart = "always"
env = { POSTGRES_DB = "app" }
""",
  )

  assert set(stack.run_units) == {"web", "db"}

  # Routes are app-level, so a route declared on one unit records which unit
  # owns it and appears once in the stack.
  assert set(stack.routes) == {"main"}
  assert stack.routes["main"].run_unit_name == "web"

  db = stack.run_units["db"]
  assert db.command == ("postgres", "-c", "max_connections=50")
  assert db.restart == "always"
  assert db.routes == {}
  assert db.environment["HAPP_RUN_UNIT"] == "db"
  assert db.environment["POSTGRES_DB"] == "app"
  assert db.labels[HARBOR_RUN_UNIT_LABEL] == "db"


def test_host_network_mode_carries_to_the_stack(tmp_path):
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

  assert stack.network_mode == "host"
