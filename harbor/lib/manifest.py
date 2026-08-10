"""Pydantic models for harbor TOML (happ manifests and catalog service definitions)."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
  BaseModel,
  ConfigDict,
  Field,
  ValidationError,
  model_validator,
)

from harbor.lib.apps import AppID
from harbor.lib.util import ROUTE_NAMESPACE, EnvTemplate, Identifier

NetworkMode = Literal["normal", "host"]
VolumeKind = Literal["app", "data", "temp", "bulk", "logs", "ext"]


class ConfigError(ValueError):
  """Raised on manifest TOML load or validation failures."""


class AppSection(BaseModel):
  # extra="allow" (unlike the other sections): app metadata is passed through verbatim
  model_config = ConfigDict(extra="allow")

  app_id: str | None = None
  version: str
  network_mode: NetworkMode = "normal"
  subdomain: Identifier | None = None
  display_name: str = ""
  description: str = ""
  main: Identifier = "main"


class VolumeEntry(BaseModel):
  model_config = ConfigDict(extra="forbid", populate_by_name=True)

  kind: VolumeKind
  src: str | None = None
  desc: str = ""
  readonly: bool = False

  @model_validator(mode="after")
  def check_src(self) -> Self:
    if self.src and self.kind != "app":
      raise ValueError("src: can only be set for app volumes")
    return self


class ConfigEntry(BaseModel):
  """A per-installation config value declared in [config].
  If a value is secret, it will be encrypted at rest.
  Every field is optional, so an entry may be declared with no options: `my_var = {}`.
  """

  model_config = ConfigDict(extra="forbid")

  desc: str = ""
  default: str | None = None
  secret: bool = False


@dataclass(frozen=True)
class PortSpec:
  """A parsed route `port` string. host_port -1 means "assign one"."""

  host_port: int
  container_port: int
  proto: Literal["tcp", "udp"]

  @classmethod
  def parse(cls, value: str) -> Self:
    ports, _, proto = value.partition("/")
    port1, separator, port2 = ports.partition(":")

    if separator:
      if not port2:
        raise ValueError(f"port {value!r}: container port is required after ':'")
      host_port = _port_number(value, port1)
      container_port = _port_number(value, port2)
    else:
      host_port = -1
      container_port = _port_number(value, port1)

    if proto and proto not in ("tcp", "udp"):
      raise ValueError(f"port {value!r}: unknown proto {proto!r}; expected tcp, udp")

    return cls(host_port, container_port, proto or "tcp")


def _port_number(value: str, text: str) -> int:
  if not text.isdigit() or not (1 <= int(text) <= 65535):
    raise ValueError(f"port {value!r}: {text!r} is not a port number (1-65535)")
  return int(text)


class RouteEntry(BaseModel):
  """A named port a run unit exposes, declared in [run.<unit>.routes].

  port    — "[host:]container[/proto]"; omit the host side so harbor assigns
            the lowest free port at/above port_base. Pin a host port only when
            absolutely necessary (see PortSpec).
  public  — when true, staging auto-assigns this route to the configured
            default_route_provider (like a config default). The operator can
            reassign later with `harbor config --route`.
  scheme  — "http" (default) or "https": what the app listens with (how a
            reverse proxy should dial the backend).
  desc    — optional human description shown in config/status output.
  """

  model_config = ConfigDict(extra="forbid")
  port: str
  public: bool = False
  scheme: Literal["http", "https"] = "http"
  desc: str = ""

  @model_validator(mode="after")
  def check_port(self) -> Self:
    PortSpec.parse(self.port)
    return self

  @property
  def port_spec(self) -> PortSpec:
    """Always parses: `check_port` rejected anything malformed at load time."""
    return PortSpec.parse(self.port)


# Compose service keys harbor generates itself; [run.<unit>.compose] may not
# shadow them -- those settings go through the dedicated manifest fields.
_COMPOSE_MANAGED_KEYS = frozenset(
  {
    "image",
    "hostname",
    "command",
    "environment",
    "volumes",
    "ports",
    "labels",
    "restart",
    "network_mode",
  }
)


class RunEntry(BaseModel):
  model_config = ConfigDict(extra="forbid")

  image: str
  cmd: list[str] | None = None
  volumes: dict[Identifier, str] = Field(default_factory=dict)
  env: dict[Identifier, str] = Field(default_factory=dict)
  routes: dict[Identifier, RouteEntry] = Field(default_factory=dict)
  restart: Literal["no", "always", "on-failure", "unless-stopped"] = "unless-stopped"
  # Escape hatch: copied verbatim into this unit's compose service for
  # anything harbor doesn't model (healthcheck, ulimits, ...).
  compose: dict[str, Any] = Field(default_factory=dict)

  @model_validator(mode="after")
  def check_compose_keys(self) -> Self:
    clashes = _COMPOSE_MANAGED_KEYS & self.compose.keys()
    if clashes:
      raise ValueError(
        "compose: harbor manages these service keys, set them via the "
        f"manifest fields instead: {', '.join(sorted(clashes))}"
      )
    return self


class CommandEntry(BaseModel):
  model_config = ConfigDict(extra="forbid")

  cmd: str | list[str]
  container: Identifier = "main"
  desc: str = ""


class Manifest(BaseModel):
  """Parsed harbor TOML for a happ bundle or catalog service definition."""

  model_config = ConfigDict(extra="forbid")

  # Happ-specific sections:
  app: AppSection

  # Shared sections:
  run: dict[Identifier, RunEntry] = Field(default_factory=dict)
  config: dict[Identifier, ConfigEntry] = Field(default_factory=dict)
  volumes: dict[Identifier, VolumeEntry] = Field(default_factory=dict)
  commands: dict[Identifier, CommandEntry] = Field(default_factory=dict)

  # Reserved:
  cron: dict[Identifier, Any] = Field(default_factory=dict)


def parse_manifest(data: bytes, app: AppID, source: Path) -> Manifest:
  """Load manifest TOML and check that it is a valid manifest for `app`.

  The only way to get a `Manifest`; `AppStack` is the only caller. Every
  failure -- unreadable bytes, bad TOML, schema violations, cross-section
  inconsistencies -- comes back as one `ConfigError` naming `source`.
  """
  try:
    manifest = Manifest.model_validate(tomllib.loads(data.decode()))
  except UnicodeDecodeError as e:
    raise ConfigError(f"manifest {source}: not valid UTF-8") from e
  except tomllib.TOMLDecodeError as e:
    raise ConfigError(f"manifest {source}: not valid TOML") from e
  except ValidationError as e:
    raise ConfigError(_fmt_validation_error(e, source)) from e

  errors = _validate_manifest(app, manifest)
  if errors:
    raise ConfigError(
      f"manifest {source}: not a valid manifest for {app}\n  " + "\n  ".join(errors)
    )
  return manifest


def _fmt_validation_error(e: ValidationError, source: Path) -> str:
  lines = [f"manifest {source}: {e.error_count()} validation error(s)"]
  for err in e.errors():
    loc = ".".join(str(p) for p in err["loc"]) or "<root>"
    msg = err["msg"]
    got = err.get("input")
    got_str = f" (got: {got!r})" if got is not None else ""
    lines.append(f"  {loc}: {msg}{got_str}")
  return "\n".join(lines)


def _validate_manifest(app: AppID, manifest: Manifest) -> list[str]:
  """Checks that span sections, which the per-section models cannot make."""
  errors: list[str] = []

  ## TODO: Don't spend much time on this now. Just get the basics, which creating a compse file might miss.
  if manifest.app.app_id is not None and manifest.app.app_id != app:
    errors.append(
      f"[app]: app_id {manifest.app.app_id!r} does not match app_id {app!r}"
    )

  main_run_unit = manifest.app.main
  if main_run_unit not in manifest.run:
    errors.append(f"[app]: main container ({main_run_unit}) not found in [run]")

  errors.extend(_validate_volumes(manifest))
  errors.extend(_validate_run_volumes(manifest))
  errors.extend(_validate_routes(manifest))
  errors.extend(_validate_env_refs(manifest))
  return errors


def _validate_env_refs(manifest: Manifest) -> list[str]:
  """`${routes.<name>}` in [run.*.env] must name a declared route.

  Every route gets a URL at compose time (provider domain when assigned,
  otherwise a harbor.localhost placeholder).
  """
  routes = {name for run_entry in manifest.run.values() for name in run_entry.routes}

  errors: list[str] = []
  for unit_name, run_entry in manifest.run.items():
    for var, value in run_entry.env.items():
      for ref in sorted(EnvTemplate(value).get_identifiers()):
        namespace, dot, route_name = ref.partition(".")
        where = f"[run.{unit_name}.env]: {var} references ${{{ref}}}"
        if not dot:
          continue  # A [config] value; `_resolve_run_units` wires those up.
        if namespace != ROUTE_NAMESPACE:
          errors.append(f"{where}, but {namespace!r} is not a known namespace")
        elif route_name not in routes:
          errors.append(f"{where}, which is not declared in [run.*.routes]")
  return errors


def _validate_volumes(manifest: Manifest) -> list[str]:
  """`app` volumes are the happ's own files: input, never state.

  A harbor app is read only, so trying to set it false is refused.
  """
  errors: list[str] = []
  for name, volume in manifest.volumes.items():
    if (
      volume.kind == "app"
      and "readonly" in volume.model_fields_set
      and not volume.readonly
    ):
      errors.append(
        f"[volumes.{name}]: app volumes are always mounted read-only; "
        "remove `readonly = false`"
      )
  return errors


def _validate_run_volumes(manifest: Manifest) -> list[str]:
  errors: list[str] = []
  for unit_name, run_entry in manifest.run.items():
    for volume_name in run_entry.volumes:
      if volume_name not in manifest.volumes:
        errors.append(
          f"[run.{unit_name}.volumes]: volume {volume_name!r} is not declared "
          "in [volumes]"
        )
  return errors


def _validate_routes(manifest: Manifest) -> list[str]:
  """Structural checks for [run.*.routes]."""
  errors: list[str] = []

  # Route names are app-level identifiers, globally unique across run units:
  # each names a published port and a subdomain label.
  route_owners: dict[str, list[str]] = {}
  for unit_name, run_entry in manifest.run.items():
    for route_name in run_entry.routes:
      route_owners.setdefault(route_name, []).append(unit_name)
  for route_name, owners in route_owners.items():
    if len(owners) > 1:
      errors.append(
        f"[run]: route name {route_name!r} is declared by multiple run units: "
        + ", ".join(owners)
      )

  has_routes = any(run_entry.routes for run_entry in manifest.run.values())

  # network_mode = "host" cannot publish ports or attach routes.
  if manifest.app.network_mode == "host":
    if has_routes:
      errors.append("[run]: network_mode 'host' forbids [run.*.routes]")

  # Routes use [app].subdomain as the DNS label base; it must be set.
  if has_routes and not manifest.app.subdomain:
    errors.append("[run.*.routes]: routes require [app].subdomain")

  return errors
