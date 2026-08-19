from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harbor.lib.apps import AppID
from harbor.lib.manifest import ConfigError, Manifest, parse_manifest

HARBOR_APP_ID_LABEL = "harbor.app_id"
HARBOR_RUN_UNIT_LABEL = "harbor.run_unit"
HARBOR_VERSION_LABEL = "harbor.version"
HARBOR_SUBDOMAIN_LABEL = "harbor.app_subdomain"

HARBOR_CONFIG_ENV_PREFIX = "__HARBOR_CONFIG_"

# The route name that maps to the bare app subdomain (rather than a
# "<name>-<appsub>" label). See docs/ingress.md.
PRIMARY_ROUTE_NAME = "main"


@dataclass(frozen=True)
class AppConfig:
  name: str
  secret: bool
  default: str | None
  desc: str | None
  hidden: bool = False

  def has_default(self) -> bool:
    return not self.secret and self.default is not None

  def env_name(self) -> str:
    return f"{HARBOR_CONFIG_ENV_PREFIX}_{self.name}"

  def __str__(self) -> str:
    return f"{self.name} ({'secret' if self.secret else 'config'})"

  def __repr__(self) -> str:
    return f"AppConfig(name={self.name}, secret={self.secret}, default={self.default}, hidden={self.hidden})"


@dataclass(frozen=True)
class AppVolume:
  name: str
  kind: str
  readonly: bool = False
  src: str | None = None

  @property
  def run_rel_path(self) -> str:
    """Where the run dir links this volume, relative to the compose file.

    Typed by kind so that "which volumes are durable" is a path glob rather
    than a manifest lookup (docs/run-layout.md L3).
    """
    return f"./volumes/{self.kind}/{self.name}"


@dataclass(frozen=True)
class BoundVolume:
  volume: AppVolume
  guest_path: str

  @property
  def readonly(self) -> bool:
    return self.volume.readonly


@dataclass(frozen=True)
class AppRoute:
  route_name: str
  run_unit_name: str
  host_port: int
  container_port: int
  proto: str
  private: bool
  scheme: Literal["http", "https"]
  desc: str = ""

  def subdomain(self, app_subdomain: str) -> str:
    prefix = "" if self.route_name == PRIMARY_ROUTE_NAME else f"{self.route_name}-"
    return f"{prefix}{app_subdomain}"

  @property
  def needs_allocation(self) -> bool:
    return self.host_port == -1


@dataclass(frozen=True)
class AppRunUnit:
  hostname: str
  image: str
  command: tuple[str, ...] | None
  environment: Mapping[str, str]
  volumes: Mapping[str, BoundVolume]
  routes: Mapping[str, AppRoute]
  labels: Mapping[str, str]
  restart: str
  compose_extra: Mapping[str, Any]


@dataclass(frozen=True)
class AppCommand:
  name: str
  argv: tuple[str, ...]
  run_unit: str
  desc: str


@dataclass(frozen=True)
class AppStack:
  """An immutable, installation-independent app definition.

  It is built only from a semantically valid manifest and app bundle. Host
  configuration such as config values, volume binds, allocated ports and
  the harbor domain belongs to :class:`AppRunData`.

  TODO: [cron] is accepted by the manifest but not resolved here yet.
  """

  app: AppID
  version: str
  network_mode: str
  subdomain: str | None
  run_units: Mapping[str, AppRunUnit]
  routes: Mapping[str, AppRoute]
  config: Mapping[str, AppConfig]
  volumes: Mapping[str, AppVolume]
  commands: Mapping[str, AppCommand]

  @classmethod
  def from_bytes(cls, data: bytes, app_id: AppID, source: Path) -> "AppStack":
    """Build from raw manifest.toml bytes; `source` only names them in errors."""
    return _build(parse_manifest(data, app_id, source), app_id)

  @classmethod
  def from_file(cls, manifest_path: Path, app_id: AppID) -> "AppStack":
    """Build from a manifest.toml on disk.

    For an installed app that is ``run/<id>/happ/manifest.toml`` (via
    ``HarborCtx.manifest_path``), never the catalog entry under ``apps/``. The
    id is explicit because the path does not always carry it (the run copy has
    neither the id nor a flavor suffix); ``happ.load_happ`` derives it for
    catalog bundles.
    """
    try:
      data = manifest_path.read_bytes()
    except OSError as e:
      raise ConfigError(f"cannot read manifest {manifest_path}: {e}") from e
    return cls.from_bytes(data, app_id, manifest_path)


def _build(manifest: Manifest, app: AppID) -> AppStack:
  config = {
    name: AppConfig(name, entry.secret, entry.default, entry.desc, entry.hidden)
    for name, entry in manifest.config.items()
  }
  # An app that names a subdomain gets it as a config key too, so the operator
  # can move it off the label the happ shipped with -- `resolved_subdomain`
  # reads the stored value back. Only when the manifest names one: an app with
  # no routes has nothing to label, and a key with no default would read as
  # unset configuration and block every start.
  if manifest.app.subdomain and "subdomain" not in config:
    config["subdomain"] = AppConfig(
      name="subdomain",
      secret=False,
      default=manifest.app.subdomain,
      desc="DNS label these routes are published under",
    )
  # `app` volumes carry the happ's own files and are always read-only, so a
  # container write fails at mount time instead of being silently discarded
  # by the next `stage` (docs/run-layout.md L4).
  volumes = {
    name: AppVolume(name, v.kind, True if v.kind == "app" else v.readonly, v.src)
    for name, v in manifest.volumes.items()
  }
  run_units = _resolve_run_units(manifest, app, volumes)
  commands = {
    name: AppCommand(
      name=name,
      argv=tuple(entry.argv()),
      run_unit=entry.run_unit,
      desc=entry.desc,
    )
    for name, entry in manifest.commands.items()
  }

  return AppStack(
    app=app,
    version=manifest.app.version,
    network_mode=manifest.app.network_mode,
    subdomain=manifest.app.subdomain,
    run_units=run_units,
    # Route names are unique across units -- `_validate_routes` rejected
    # anything else before we got here.
    routes={
      name: route for unit in run_units.values() for name, route in unit.routes.items()
    },
    config=config,
    volumes=volumes,
    commands=commands,
  )


def _resolve_run_units(
  manifest: Manifest,
  app: AppID,
  volumes: Mapping[str, AppVolume],
) -> Mapping[str, AppRunUnit]:
  run_units = {}

  for run_unit_name, run_entry in manifest.run.items():
    # Placeholders in env stay as written; `make_compose_dict` substitutes
    # against the flat keyspace (config, routes.*, happ.*).
    run_env = {
      "HAPP_ID": app,
      "HAPP_VERSION": manifest.app.version,
      "HAPP_RUN_UNIT": run_unit_name,
      **{str(k): str(v) for k, v in run_entry.env.items()},
    }

    run_units[run_unit_name] = AppRunUnit(
      hostname=run_unit_name,
      image=run_entry.image,
      command=tuple(run_entry.cmd) if run_entry.cmd else None,
      environment=run_env,
      volumes={
        name: BoundVolume(volumes[name], guest_path)
        for name, guest_path in run_entry.volumes.items()
      },
      routes={
        name: AppRoute(
          route_name=name,
          run_unit_name=run_unit_name,
          host_port=route.port_spec.host_port,
          container_port=route.port_spec.container_port,
          proto=route.port_spec.proto,
          private=route.private,
          scheme=route.scheme,
          desc=route.desc,
        )
        for name, route in run_entry.routes.items()
      },
      labels={
        HARBOR_APP_ID_LABEL: app,
        HARBOR_VERSION_LABEL: manifest.app.version,
        HARBOR_RUN_UNIT_LABEL: run_unit_name,
      },
      restart=run_entry.restart,
      compose_extra=run_entry.compose,
    )

  return run_units
