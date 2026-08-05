"""Pydantic models for harbor TOML (happ manifests and catalog service definitions)."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
  BaseModel,
  ConfigDict,
  Field,
  PrivateAttr,
  ValidationError,
  model_validator,
)

from harbor.lib.apps import AppID
from harbor.lib.util import Identifier

NetworkMode = Literal["normal", "host"]
VolumeKind = Literal["app", "data", "temp", "bulk", "logs", "ext"]


class ConfigError(Exception):
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


class RouteEntry(BaseModel):
  """A named port a run unit publishes, declared in [run.<unit>.routes].

  port    — "[host:]container[/proto]"; omit the host side so harbor assigns
            the lowest free port at/above port_base. Pin a host port only when
            absolutely necessary (see PortSpec).
  publish — "lan" (default): publish the host port only; "web": additionally
            register a reverse-proxy route. The route name is the subdomain
            label (the reserved name "main" → the bare app subdomain).
  scheme  — "http" (default) or "https": how the reverse proxy dials the
            backend. Only meaningful for publish = "web".
  """

  model_config = ConfigDict(extra="forbid")
  port: str
  publish: Literal["web", "lan", "none"] = "none"
  scheme: Literal["http", "https"] = "http"


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

  # Not parsed - assigned after validation
  _app_handle: AppID | None = PrivateAttr(default=None)

  @property
  def app_handle(self) -> AppID:
    if self._app_handle is None:
      raise ValueError("App handle not set")
    return self._app_handle


def _fmt_validation_error(e: ValidationError, source: str) -> str:
  lines = [f"manifest {source!r}: {e.error_count()} validation error(s)"]
  for err in e.errors():
    loc = ".".join(str(p) for p in err["loc"]) or "<root>"
    msg = err["msg"]
    got = err.get("input")
    got_str = f" (got: {got!r})" if got is not None else ""
    lines.append(f"  {loc}: {msg}{got_str}")
  return "\n".join(lines)


def parse_manifest_bytes(data: bytes, source_path: Path) -> Manifest:
  """Parse and validate a manifest from raw TOML bytes (path not set)."""
  source_desc = str(source_path)

  try:
    raw = tomllib.loads(data.decode())
    doc = Manifest.model_validate(raw)
    return doc
  except UnicodeDecodeError as e:
    raise ConfigError(f"manifest {source_desc}: not valid UTF-8") from e
  except tomllib.TOMLDecodeError as e:
    raise ConfigError(f"manifest {source_desc}: not valid TOML") from e
  except ValidationError as e:
    raise ConfigError(_fmt_validation_error(e, source_desc)) from e
  except ValueError as e:
    raise ConfigError(f"manifest {source_desc}: {e}") from e


def parse_manifest(manifest_path: str | Path) -> Manifest:
  try:
    path = Path(manifest_path)
    with open(path, "rb") as f:
      return parse_manifest_bytes(f.read(), path.resolve().parent)
  except FileNotFoundError as e:
    raise ConfigError(f"manifest not found: {manifest_path}") from e


def _validate_manifest(app: AppID, manifest: Manifest) -> list[str]:
  """Ensure that the Manifest is valid & correct for the given AppHandle"""
  errors: list[str] = []
  app_id = app

  try:
    ## TODO: Don't spend much time on this now. Just get the basics, which creating a compse file might miss.
    if manifest.app.app_id is not None and manifest.app.app_id != app:
      errors.append(
        f"[app]: app_id {manifest.app.app_id!r} does not match app_id {app_id!r}"
      )

    main_run_unit = manifest.app.main
    if main_run_unit not in manifest.run:
      errors.append(f"[app]: main container ({main_run_unit}) not found in [run]")

    errors.extend(_validate_volumes(manifest))
    errors.extend(_validate_run_volumes(manifest))
    errors.extend(_validate_routes(manifest))

    return errors
  except ConfigError as e:
    errors.append(str(e))
    return [str(e)]


def _validate_volumes(manifest: Manifest) -> list[str]:
  """`app` volumes are the happ's own files: input, never state.

  Harbor mounts every one of them read-only, so an explicit `readonly = false`
  is refused rather than silently reversed -- the author wrote it meaning
  something, and should be told it is impossible (docs/run-layout.md L4).
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
  """Structural checks for [run.*.routes] (see docs/ingress.md §2)."""
  errors: list[str] = []

  # Route names are app-level identifiers, globally unique across run units:
  # each names a published port and (for web routes) a subdomain label.
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

  web_routes = [
    route_name
    for run_entry in manifest.run.values()
    for route_name, route in run_entry.routes.items()
    if route.publish == "web"
  ]

  # network_mode = "host" cannot publish ports or attach routes.
  if manifest.app.network_mode == "host":
    if any(run_entry.routes for run_entry in manifest.run.values()):
      errors.append("[run]: network_mode 'host' forbids [run.*.routes]")

  # web routes are published under the app subdomain; it must be set.
  if web_routes and not manifest.app.subdomain:
    errors.append(
      "[run.*.routes]: web routes require [app].subdomain; "
      "these are web-facing: " + ", ".join(sorted(web_routes))
    )

  return errors


@dataclass
class ManifestParseFailure:
  errors: list[str]


ManifestParseResult = Manifest | ManifestParseFailure


def app_to_manifest(app: AppID, app_path: Path) -> ManifestParseResult:
  try:
    manifest = parse_manifest(app_path / "manifest.toml")
    errors = _validate_manifest(app, manifest)

    if errors:
      return ManifestParseFailure(errors)
    manifest._app_handle = app
    return manifest
  except ConfigError as e:
    return ManifestParseFailure([str(e)])
