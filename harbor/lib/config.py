import logging
import os
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
from harbor.lib.logtab import LogTab
from harbor.lib.util import validate_identifier

VOLUME_KINDS = ("data", "temp", "bulk", "logs")

# The name of the app source backed by `apps_root`. Always present, always
# first, and the only one harbor itself writes to (`fetch`, and the symlink
# `stage <path>` leaves behind).
DEFAULT_APP_SOURCE = "apps"

# Built-in noop provider tag. Always present; operators may not redefine it.
NONE_ROUTE_PROVIDER_TAG = "none"
# Domain used for route URLs when a route is unassigned or assigned to none.
PLACEHOLDER_DOMAIN = "harbor.localhost"

RouteProviderKind = Literal["nginx_proxy_manager", "pangolin", "noop"]


logger = logging.getLogger("harbor.config")


def _expand_path(
  path: str, relative_base: Path, harbor_root: Path | None = None
) -> Path:
  if harbor_root is not None:
    path = path.replace("${harbor_root}", str(harbor_root))

  if path.startswith("~"):
    return Path(path).expanduser().resolve()

  p = Path(path)
  if p.is_absolute():
    return p.resolve()

  return (relative_base / p).resolve()


class AppSourceEntry(BaseModel):
  model_config = ConfigDict(extra="forbid")

  name: str
  location: str


class RouteProviderEntry(BaseModel):
  """One `[route_provider.<tag>]` table.

  ``args`` are kind-specific and passed through to that provider's constructor
  (after resolving any secrets the kind requires).
  """

  model_config = ConfigDict(extra="forbid")

  kind: RouteProviderKind
  domain: str
  args: dict[str, str] = Field(default_factory=dict)


class HostVolumeEntry(BaseModel):
  """One `[host_volume.<tag>]` table (path still a string; expanded later)."""

  model_config = ConfigDict(extra="forbid")

  path: str
  readonly: bool = False
  require_mount: bool = False


class VolumeRootsEntry(BaseModel):
  model_config = ConfigDict(extra="forbid")

  data: str
  temp: str
  bulk: str
  logs: str


class ConfigFile(BaseModel):
  """Shape of config.toml. Cross-cutting checks live in `_validate_config`."""

  model_config = ConfigDict(extra="forbid")

  apps_root: str = "apps"
  run_root: str = "run"
  snapshot_root: str = "snapshots"
  master_keyfile: str = "master.key"
  port_base: int = 41000
  volume_root: str | None = None
  volume_roots: VolumeRootsEntry | None = None
  harbor_address: str = ""
  default_route_provider: str = NONE_ROUTE_PROVIDER_TAG
  route_provider: dict[str, RouteProviderEntry] = Field(default_factory=dict)
  host_volume: dict[str, HostVolumeEntry] = Field(default_factory=dict)

  @model_validator(mode="after")
  def volume_root_xor(self) -> Self:
    if self.volume_root is not None and self.volume_roots is not None:
      raise ValueError(
        "Specify either 'volume_root' or individual 'volume_roots', not both"
      )
    if self.volume_root is None and self.volume_roots is None:
      raise ValueError("Specify 'volume_root' or individual 'volume_roots'")
    return self


@dataclass(frozen=True)
class HostVolume:
  """A tagged host path apps may bind a `kind = "host"` volume to."""

  tag: str
  path: Path
  readonly: bool = False
  # When true, refuse unless `path` is an active mount point (NFS, etc.).
  # The bare path can exist as an empty directory when the mount is down.
  require_mount: bool = False


class Config:
  harbor_root: Path
  volume_roots: dict[str, Path]
  apps_root: Path
  app_sources: dict[str, Path]
  run_root: Path
  master_key: str
  master_keyfile: Path
  port_base: int
  harbor_address: str
  default_route_provider: str
  route_providers: dict[str, RouteProviderEntry]
  host_volumes: dict[str, HostVolume]

  def __init__(
    self,
    harbor_root: Path,
    volume_roots: dict[str, Path],
    apps_root: Path,
    run_root: Path,
    snapshot_root: Path,
    master_key: str,
    master_keyfile: Path,
    port_base: int,
    default_route_provider: str,
    route_providers: dict[str, RouteProviderEntry],
    harbor_address: str = "",
    extra_app_sources: dict[str, Path] | None = None,
    host_volumes: dict[str, HostVolume] | None = None,
  ) -> None:
    self.harbor_root = harbor_root
    self.volume_roots = volume_roots
    self.apps_root = apps_root
    self.app_sources = {DEFAULT_APP_SOURCE: apps_root, **(extra_app_sources or {})}
    self.run_root = run_root
    self.snapshot_root = snapshot_root
    self.master_key = master_key
    self.master_keyfile = master_keyfile
    self.port_base = port_base
    self.harbor_address = harbor_address
    self.default_route_provider = default_route_provider
    self.route_providers = route_providers
    self.host_volumes = host_volumes or {}

  @property
  def harbor_lockfile_path(self) -> Path:
    return self.harbor_root / "harbor.lock"

  @property
  def harbordb_path(self) -> Path:
    return self.harbor_root / "harbordb.logtab"

  @property
  def activity_log(self) -> Path:
    return self.harbor_root / "activity.logtab"

  def app_run_path(self, app_id: AppID) -> Path:
    return self.run_root / app_id

  @property
  def config_root(self) -> Path:
    return self.harbor_root / "config"

  def app_config_path(self, app_id: AppID | str) -> Path:
    return self.config_root / f"{app_id}.logtab"

  def app_config_ids(self) -> set[str]:
    """App ids that have a config logtab under ``config/``."""
    root = self.config_root
    if not root.is_dir():
      return set()
    found: set[str] = set()
    for path in root.iterdir():
      if path.suffix != ".logtab" or not path.is_file():
        continue
      app_id = path.name.removesuffix(".logtab")
      try:
        AppID(app_id)
      except ValueError:
        continue
      found.add(app_id)
    return found

  def provider_domain(self, tag: str) -> str:
    """Domain for a route-provider tag; placeholder when unassigned/none."""
    if not tag or tag == NONE_ROUTE_PROVIDER_TAG:
      return PLACEHOLDER_DOMAIN
    conf = self.route_providers.get(tag)
    if conf is None:
      return PLACEHOLDER_DOMAIN
    return conf.domain


def load_config_file(config_file: str | Path) -> Config:
  config_path = Path(config_file).resolve()
  config_dir = config_path.parent
  harbor_root = config_dir

  with open(config_path, "rb") as f:
    data = tomllib.load(f)

  # Soft-fail section: validated after the hard schema so a typo here cannot
  # take down every harbor command (see `_resolve_app_sources`).
  app_source_raw = data.get("app_source", [])
  parse_data = {k: v for k, v in data.items() if k != "app_source"}

  try:
    parsed = ConfigFile.model_validate(parse_data)
  except ValidationError as e:
    raise ValueError(_fmt_validation_error(e, config_path)) from e

  errors = _validate_config(parsed)
  if errors:
    raise ValueError(f"config {config_path}: not valid\n  " + "\n  ".join(errors))

  def ep(p: str) -> Path:
    return _expand_path(p, config_dir, harbor_root)

  if parsed.volume_root is not None:
    vr = ep(parsed.volume_root)
    volume_roots = {kind: vr / kind for kind in VOLUME_KINDS}
  else:
    assert parsed.volume_roots is not None
    volume_roots = {
      kind: ep(getattr(parsed.volume_roots, kind)) for kind in VOLUME_KINDS
    }

  master_keyfile = ep(parsed.master_keyfile)

  # NB: Should we technically hold the lock here? Eh.
  master_key_entry = LogTab(master_keyfile).read("master_key")
  master_key = master_key_entry.value if master_key_entry else ""

  if master_key:
    logger.debug(f"Using master key from {master_keyfile}")
  else:
    logger.warning("Using empty master key")

  apps_root = ep(parsed.apps_root)
  extra_app_sources = _resolve_app_sources(app_source_raw, apps_root, ep)
  run_root = ep(parsed.run_root)
  snapshot_root = ep(parsed.snapshot_root)

  route_providers: dict[str, RouteProviderEntry] = {
    NONE_ROUTE_PROVIDER_TAG: RouteProviderEntry(kind="noop", domain=PLACEHOLDER_DOMAIN),
    **parsed.route_provider,
  }

  host_volumes = {
    tag: HostVolume(
      tag=tag,
      path=ep(entry.path),
      readonly=entry.readonly,
      require_mount=entry.require_mount,
    )
    for tag, entry in parsed.host_volume.items()
  }

  return Config(
    harbor_root=harbor_root,
    volume_roots=volume_roots,
    apps_root=apps_root,
    run_root=run_root,
    snapshot_root=snapshot_root,
    master_key=master_key,
    master_keyfile=master_keyfile,
    port_base=parsed.port_base,
    harbor_address=parsed.harbor_address,
    default_route_provider=parsed.default_route_provider,
    route_providers=route_providers,
    extra_app_sources=extra_app_sources,
    host_volumes=host_volumes,
  )


def _fmt_validation_error(e: ValidationError, source: Path) -> str:
  lines = [f"config {source}: {e.error_count()} validation error(s)"]
  for err in e.errors():
    loc = ".".join(str(p) for p in err["loc"]) or "<root>"
    msg = err["msg"]
    got = err.get("input")
    got_str = f" (got: {got!r})" if got is not None else ""
    lines.append(f"  {loc}: {msg}{got_str}")
  return "\n".join(lines)


def _validate_config(parsed: ConfigFile) -> list[str]:
  """Cross-cutting checks the per-section models cannot make alone."""
  errors: list[str] = []

  if NONE_ROUTE_PROVIDER_TAG in parsed.route_provider:
    errors.append(
      f"route_provider tag {NONE_ROUTE_PROVIDER_TAG!r} is reserved; "
      f"remove [route_provider.{NONE_ROUTE_PROVIDER_TAG}] from config.toml"
    )

  for tag in parsed.route_provider:
    try:
      validate_identifier(tag)
    except ValueError as e:
      errors.append(f"route_provider tag {tag!r} is not a valid name: {e}")

  # Every provider that actually proxies traffic has to be told where harbor
  # is; only noop, which configures nothing, can do without it.
  if not parsed.harbor_address:
    proxying = sorted(
      tag for tag, entry in parsed.route_provider.items() if entry.kind != "noop"
    )
    if proxying:
      errors.append(
        f"route_provider {proxying} need harbor_address to point at; "
        f'set harbor_address = "<ip or hostname>" in config.toml'
      )

  for tag in parsed.host_volume:
    try:
      validate_identifier(tag)
    except ValueError as e:
      errors.append(f"host_volume tag {tag!r} is not a valid name: {e}")

  default = parsed.default_route_provider
  if not (isinstance(default, str) and default):
    errors.append(
      "default_route_provider must be a non-empty string tag "
      f"(or omit it for {NONE_ROUTE_PROVIDER_TAG!r})"
    )
  else:
    known = {NONE_ROUTE_PROVIDER_TAG, *parsed.route_provider}
    if default not in known:
      errors.append(
        f"default_route_provider {default!r} is not a configured route_provider; "
        f"add [route_provider.{default}] or set default_route_provider to a known tag"
      )

  return errors


def _resolve_app_sources(entries: Any, apps_root: Path, ep) -> dict[str, Path]:
  """The `[[app_source]]` blocks that add app directories beyond `apps/`.

  Names and locations must both be unique. Errors soft-fail: a typo in this
  optional section must not stop every harbor command.
  """

  def refuse(problem: str) -> dict[str, Path]:
    logger.error(
      "%s. Ignoring every [[app_source]] until config.toml is fixed", problem
    )
    return {}

  if not isinstance(entries, list):
    return refuse("app_source must be a list of [[app_source]] tables")

  sources: dict[str, Path] = {}
  names_by_path = {apps_root: DEFAULT_APP_SOURCE}

  for entry in entries:
    try:
      parsed = AppSourceEntry.model_validate(entry)
    except ValidationError:
      return refuse(
        'each [[app_source]] needs a name and a location, e.g. name = "dev", '
        'location = "~/code/happs"'
      )

    try:
      validate_identifier(parsed.name)
    except ValueError as e:
      return refuse(f"app_source name {parsed.name!r} is not a valid name: {e}")

    if parsed.name == DEFAULT_APP_SOURCE or parsed.name in sources:
      return refuse(
        f"app_source {parsed.name!r} is defined twice (or collides with the "
        f"built-in {DEFAULT_APP_SOURCE!r} source at {apps_root}); give it "
        f"another name"
      )

    path = ep(parsed.location)
    if path in names_by_path:
      return refuse(
        f"app_source {parsed.name!r} points at {path}, which is already the "
        f"{names_by_path[path]!r} source; every app there would resolve twice"
      )

    names_by_path[path] = parsed.name
    sources[parsed.name] = path

  return sources


CONFIG_LOCATIONS = [
  Path("~/.harbor/config.toml"),
  Path("/etc/harbor/config.toml"),
]


def load_config(
  *,
  config_path: str | Path | None = None,
  root: str | Path | None = None,
) -> Config | None:
  """Find and load the harbor config.

  Precedence: ``config_path`` / ``root`` arguments, then ``HARBOR_CONFIG`` /
  ``HARBOR_ROOT`` env, then the standard locations. An explicit override must
  point at an existing config file; if it doesn't, that's an error. With no
  override set, fall back to the standard locations, returning None if none
  exist.
  """
  if config_path is not None:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
      raise RuntimeError(f"No config file exists at {path}")
    logger.debug(f"Loading from {path}")
    return load_config_file(path)

  if root is not None:
    path = (Path(root).expanduser() / "config.toml").resolve()
    if not path.is_file():
      raise RuntimeError(f"No config file exists at {path}")
    logger.debug(f"Loading from {path}")
    return load_config_file(path)

  if os.environ.get("HARBOR_CONFIG"):
    path = Path(os.environ["HARBOR_CONFIG"]).expanduser().resolve()
    if not path.is_file():
      raise RuntimeError(
        f"HARBOR_CONFIG is set to {path}, but no config file exists there"
      )
    logger.debug(f"Loading from {path}")
    return load_config_file(path)

  if os.environ.get("HARBOR_ROOT"):
    path = (Path(os.environ["HARBOR_ROOT"]).expanduser() / "config.toml").resolve()
    if not path.is_file():
      raise RuntimeError(f"HARBOR_ROOT is set, but no config file exists at {path}")
    logger.debug(f"Loading from {path}")
    return load_config_file(path)

  for candidate in CONFIG_LOCATIONS:
    path = candidate.expanduser().resolve()
    if path.is_file():
      logger.debug(f"Loading from {path}")
      return load_config_file(path)

  searched = ", ".join(str(c.expanduser().resolve()) for c in CONFIG_LOCATIONS)
  logger.warning(f"No harbor config found. Searched: {searched}")
  return None
