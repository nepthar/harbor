import logging
import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from harbor.lib.apps import AppID, app_id_from_path
from harbor.lib.manifest import (
  Manifest,
  ManifestParseFailure,
  app_to_manifest,
)

logger = logging.getLogger("harbor.stack")

HARBOR_APP_ID_LABEL = "harbor.app_id"
HARBOR_RUN_UNIT_LABEL = "harbor.run_unit"
HARBOR_VERSION_LABEL = "harbor.version"
HARBOR_SUBDOMAIN_LABEL = "harbor.app_subdomain"

HARBOR_CONFIG_ENV_PREFIX = "__HARBOR_CONFIG_"

# The route name that maps to the bare app subdomain (rather than a
# "<name>-<appsub>" label). See docs/ingress.md.
PRIMARY_ROUTE_NAME = "main"


@dataclass(frozen=True)
class PortSpec:
  host_port: int
  container_port: int
  proto: Literal["tcp", "udp"]

  @classmethod
  def parse(cls, value: str) -> Self:
    ports, _, proto = value.partition("/")
    port1, separator, port2 = ports.partition(":")

    if separator:
      if not port2:
        raise ValueError("container port is required after ':'")
      host_port = int(port1)
      container_port = int(port2)
    else:
      host_port = -1
      container_port = int(port1)

    if proto and proto not in ("tcp", "udp"):
      raise ValueError(f"unknown proto restriction: {proto!r}; expected tcp, udp")

    if not (1 <= container_port <= 65535):
      raise ValueError(f"container port {container_port!r} must be between 1 and 65535")

    if host_port != -1 and not (1 <= host_port <= 65535):
      raise ValueError(f"host port {host_port!r} must be -1 or between 1 and 65535")

    return cls(host_port, container_port, proto or "tcp")


@dataclass(frozen=True)
class AppConfig:
  name: str
  secret: bool
  default: str | None
  desc: str | None

  def has_default(self) -> bool:
    return not self.secret and self.default is not None

  def env_name(self) -> str:
    return f"{HARBOR_CONFIG_ENV_PREFIX}_{self.name}"

  def __str__(self) -> str:
    return f"{self.name} ({'secret' if self.secret else 'config'})"

  def __repr__(self) -> str:
    return f"AppConfig(name={self.name}, secret={self.secret}, default={self.default})"


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
class ExposedRoute:
  host_port: int
  container_port: int
  proto: str
  publish: Literal["web", "lan"]
  scheme: Literal["http", "https"]

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
  routes: Mapping[str, ExposedRoute]
  labels: Mapping[str, str]
  restart: str


@dataclass(frozen=True)
class AppCommand:
  cmd: tuple[str, ...]
  container: AppRunUnit
  desc: str


@dataclass(frozen=True)
class AppCronJob:
  # TODO: Future
  pass


@dataclass(frozen=True)
class AppRoute:
  route_name: str
  run_unit_name: str
  host_port: int
  container_port: int
  proto: str
  publish: Literal["web", "lan"]
  scheme: Literal["http", "https"]

  def subdomain(self, app_subdomain: str) -> str:
    prefix = "" if self.route_name == PRIMARY_ROUTE_NAME else f"{self.route_name}-"
    return f"{prefix}{app_subdomain}"

  @property
  def needs_allocation(self) -> bool:
    return self.host_port == -1


@dataclass(frozen=True)
class AppStack:
  """An immutable, installation-independent app definition.

  It is built only from a semantically valid manifest and app bundle. Host
  configuration such as config values, volume binds, allocated ports and
  the harbor domain belongs to :class:`AppRunData`.
  """

  app: AppID
  network_mode: str
  subdomain: str | None
  run_units: Mapping[str, AppRunUnit]
  commands: Mapping[str, AppCommand]
  cron_jobs: Mapping[str, AppCronJob]
  routes: Mapping[str, AppRoute]
  config: Mapping[str, AppConfig]
  volumes: Mapping[str, AppVolume]

  @property
  def ports(self) -> Mapping[str, ExposedRoute]:
    ports: dict[str, ExposedRoute] = {}
    for unit in self.run_units.values():
      ports.update(unit.routes)
    return ports


def _base_env(manifest: Manifest, run_unit_name: str) -> dict[str, str]:
  return {
    "HAPP_ID": manifest.app_handle,
    "HAPP_VERSION": manifest.app.version,
    "HAPP_RUN_UNIT": run_unit_name,
  }


def _resolve_run_units(
  manifest: Manifest,
  config: Mapping[str, AppConfig],
  volumes: Mapping[str, AppVolume],
) -> Mapping[str, AppRunUnit]:
  run_units = {}

  config_varnames = {str(name): f"${{{c.env_name()}}}" for name, c in config.items()}

  for run_unit_name, run_entry in manifest.run.items():
    run_env = _base_env(manifest, run_unit_name)
    run_env.update(run_entry.env)
    run_env = {
      k: string.Template(str(v)).safe_substitute(config_varnames)
      for k, v in run_env.items()
    }

    labels = {
      HARBOR_APP_ID_LABEL: manifest.app_handle,
      HARBOR_VERSION_LABEL: manifest.app.version,
      HARBOR_RUN_UNIT_LABEL: run_unit_name,
    }

    bind_mounts = {}
    for volname, guest_path in run_entry.volumes.items():
      volume = volumes[volname]
      bind_mounts[volname] = BoundVolume(volume, guest_path)

    unit_routes = {}
    for route_name, route_entry in run_entry.routes.items():
      try:
        port_spec = PortSpec.parse(route_entry.port)
      except ValueError as e:
        raise ValueError(
          f"Invalid port specification for route {route_name}: {e}"
        ) from e

      unit_routes[route_name] = AppRoute(
        route_name=route_name,
        run_unit_name=run_unit_name,
        host_port=port_spec.host_port,
        container_port=port_spec.container_port,
        proto=port_spec.proto,
        publish=route_entry.publish,
        scheme=route_entry.scheme,
      )

    run_units[run_unit_name] = AppRunUnit(
      hostname=run_unit_name,
      image=run_entry.image,
      command=tuple(run_entry.cmd) if run_entry.cmd else None,
      environment=run_env,
      volumes=bind_mounts,
      routes=unit_routes,
      labels=labels,
      restart=run_entry.restart,
    )

  return run_units


def _resolve_volumes(manifest: Manifest) -> Mapping[str, AppVolume]:
  volumes = {}
  for volume_name, volume in manifest.volumes.items():
    # `app` volumes carry the happ's own files and are always read-only, so a
    # container write fails at mount time instead of being silently discarded
    # by the next `stage` (docs/run-layout.md L4).
    readonly = True if volume.kind == "app" else volume.readonly
    volumes[volume_name] = AppVolume(volume_name, volume.kind, readonly, volume.src)
  return volumes


def _resolve_routes(run_units: Mapping[str, AppRunUnit]):
  all_routes = {}
  for run_unit in run_units.values():
    for route_name, route_entry in run_unit.routes.items():
      if route_name in all_routes:
        raise ValueError(f"Route {route_name} is defined multiple times")
      all_routes[route_name] = route_entry
  return all_routes


def _resolve_config(manifest: Manifest) -> Mapping[str, AppConfig]:
  config = {}
  for config_name, config_entry in manifest.config.items():
    config[config_name] = AppConfig(
      config_name, config_entry.secret, config_entry.default, config_entry.desc
    )
  return config


def _resolve_cron_jobs(manifest: Manifest) -> Mapping[str, AppCronJob]:
  logger.debug("TODO: Manifest cron jobs")
  return {}


def _resolve_commands(manifest: Manifest) -> Mapping[str, AppCommand]:
  logger.debug("TODO: Maifest commands")
  return {}


def build_app_stack(manifest: Manifest) -> AppStack:
  app = manifest.app_handle

  config = _resolve_config(manifest)
  volumes = _resolve_volumes(manifest)
  run_units = _resolve_run_units(manifest, config, volumes)
  routes = _resolve_routes(run_units)
  cron_jobs = _resolve_cron_jobs(manifest)
  commands = _resolve_commands(manifest)

  return AppStack(
    app=app,
    network_mode=manifest.app.network_mode,
    subdomain=manifest.app.subdomain,
    run_units=run_units,
    commands=commands,
    cron_jobs=cron_jobs,
    routes=routes,
    config=config,
    volumes=volumes,
  )


def app_stack(app_path: Path, app_id: AppID | None = None) -> AppStack:
  """Parse and validate an app bundle into an AppStack.

  For an installed app, pass ``run/<id>/happ`` (via ``HarborCtx.app_path``),
  never the catalog entry under ``apps/``. Pass ``app_id`` when the directory
  name does not carry the id (the run copy has neither the id nor a ``.happ``
  suffix).
  """
  app_id = app_id if app_id is not None else app_id_from_path(app_path)
  manifest = app_to_manifest(app_id, app_path)
  match manifest:
    case Manifest():
      return build_app_stack(manifest)
    case ManifestParseFailure(errors):
      detail = "\n".join(errors)
      raise ValueError(f"App {app_path} does not contain a valid manifest:\n{detail}")
