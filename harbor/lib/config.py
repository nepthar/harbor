import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class HostVolume:
  """A tagged host path apps may bind a `kind = "host"` volume to."""

  tag: str
  path: Path
  readonly: bool = False


class Config:
  harbor_root: Path
  volume_roots: dict[str, Path]
  apps_root: Path
  app_sources: dict[str, Path]
  run_root: Path
  master_key: str
  master_keyfile: Path
  port_base: int
  default_route_provider: str
  route_providers: dict[str, dict]
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
    port_base: int = 41000,
    default_route_provider: str = NONE_ROUTE_PROVIDER_TAG,
    route_providers: dict[str, dict] | None = None,
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
    self.default_route_provider = default_route_provider
    self.route_providers = route_providers or {
      NONE_ROUTE_PROVIDER_TAG: {"kind": "noop", "domain": PLACEHOLDER_DOMAIN}
    }
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

  def provider_domain(self, tag: str) -> str:
    """Domain for a route-provider tag; placeholder when unassigned/none."""
    if not tag or tag == NONE_ROUTE_PROVIDER_TAG:
      return PLACEHOLDER_DOMAIN
    conf = self.route_providers.get(tag)
    if conf is None:
      return PLACEHOLDER_DOMAIN
    return conf.get("domain", PLACEHOLDER_DOMAIN)


def load_config_file(config_file: str | Path) -> Config:
  config_path = Path(config_file).resolve()
  config_dir = config_path.parent
  harbor_root = config_dir

  with open(config_path, "rb") as f:
    data = tomllib.load(f)

  if "domain" in data:
    raise ValueError(
      "top-level 'domain' is no longer valid; set domain on each "
      "[route_provider.<tag>] instead"
    )

  if "volume_root" in data and "volume_roots" in data:
    raise ValueError(
      "Specify either 'volume_root' or individual 'volume_roots', not both"
    )

  def ep(p: str) -> Path:
    return _expand_path(p, config_dir, harbor_root)

  if "volume_root" in data:
    vr = ep(data["volume_root"])
    volume_roots = {kind: vr / kind for kind in VOLUME_KINDS}
  else:
    vrs = data["volume_roots"]
    volume_roots = {kind: ep(vrs[kind]) for kind in VOLUME_KINDS}

  master_keyfile = ep(data.get("master_keyfile", "master.key"))

  # NB: Should we technically hold the lock here? Eh.
  master_key_entry = LogTab(master_keyfile).read("master_key")
  master_key = master_key_entry.value if master_key_entry else ""

  if master_key:
    logger.debug(f"Using master key from {master_keyfile}")
  else:
    logger.warning("Using empty master key")

  apps_root = ep(data.get("apps_root", "apps"))
  extra_app_sources = _parse_app_sources(data.get("app_source", []), apps_root, ep)
  run_root = ep(data.get("run_root", "run"))
  snapshot_root = ep(data.get("snapshot_root", "snapshots"))
  port_base = data.get("port_base", 41000)
  default_route_provider, route_providers = _parse_route_providers(data)
  host_volumes = _parse_host_volumes(data, ep)

  return Config(
    harbor_root=harbor_root,
    volume_roots=volume_roots,
    apps_root=apps_root,
    run_root=run_root,
    snapshot_root=snapshot_root,
    master_key=master_key,
    master_keyfile=master_keyfile,
    port_base=port_base,
    default_route_provider=default_route_provider,
    route_providers=route_providers,
    extra_app_sources=extra_app_sources,
    host_volumes=host_volumes,
  )


def _parse_route_providers(data: dict) -> tuple[str, dict[str, dict]]:
  """Parse `[route_provider.<tag>]` tables and `default_route_provider`.

  Always injects the reserved `none` noop provider. A user-defined `none` tag
  is a configuration error.
  """
  raw = data.get("route_provider", {})
  if raw is None:
    raw = {}
  if not isinstance(raw, dict):
    raise ValueError("route_provider must be a table of [route_provider.<tag>] entries")

  if NONE_ROUTE_PROVIDER_TAG in raw:
    raise ValueError(
      f"route_provider tag {NONE_ROUTE_PROVIDER_TAG!r} is reserved; "
      f"remove [route_provider.{NONE_ROUTE_PROVIDER_TAG}] from config.toml"
    )

  providers: dict[str, dict] = {
    NONE_ROUTE_PROVIDER_TAG: {"kind": "noop", "domain": PLACEHOLDER_DOMAIN},
  }

  for tag, conf in raw.items():
    try:
      validate_identifier(tag)
    except ValueError as e:
      raise ValueError(f"route_provider tag {tag!r} is not a valid name: {e}") from e
    if not isinstance(conf, dict):
      raise ValueError(
        f"route_provider.{tag}: expected a table, e.g. "
        f'[route_provider.{tag}] with kind = "nginx_proxy_manager"'
      )
    kind = conf.get("kind")
    if not _nonempty_str(kind):
      raise ValueError(
        f'route_provider.{tag}: missing required key "kind" '
        f'(e.g. kind = "nginx_proxy_manager")'
      )
    if kind not in ("nginx_proxy_manager", "noop"):
      raise ValueError(
        f"route_provider.{tag}: unknown kind {kind!r}; "
        f'expected "nginx_proxy_manager" or "noop"'
      )
    domain = conf.get("domain")
    if kind != "noop" and not _nonempty_str(domain):
      raise ValueError(f'route_provider.{tag}: missing required key "domain"')
    entry = dict(conf)
    if not _nonempty_str(entry.get("domain")):
      entry["domain"] = PLACEHOLDER_DOMAIN
    providers[tag] = entry

  default = data.get("default_route_provider", NONE_ROUTE_PROVIDER_TAG)
  if not _nonempty_str(default):
    raise ValueError(
      "default_route_provider must be a non-empty string tag "
      f"(or omit it for {NONE_ROUTE_PROVIDER_TAG!r})"
    )
  if default not in providers:
    raise ValueError(
      f"default_route_provider {default!r} is not a configured route_provider; "
      f"add [route_provider.{default}] or set default_route_provider to a known tag"
    )

  return default, providers


def _nonempty_str(value) -> bool:
  return isinstance(value, str) and bool(value)


def _parse_host_volumes(data: dict, ep) -> dict[str, HostVolume]:
  """Parse `[host_volume.<tag>]` tables into tagged host paths."""
  raw = data.get("host_volume", {})
  if raw is None:
    return {}
  if not isinstance(raw, dict):
    raise ValueError(
      "host_volume must be a table of [host_volume.<tag>] entries, e.g. "
      '[host_volume.media] with path = "/mnt/media"'
    )

  volumes: dict[str, HostVolume] = {}
  for tag, conf in raw.items():
    try:
      validate_identifier(tag)
    except ValueError as e:
      raise ValueError(f"host_volume tag {tag!r} is not a valid name: {e}") from e
    if not isinstance(conf, dict):
      raise ValueError(
        f"host_volume.{tag}: expected a table, e.g. "
        f'[host_volume.{tag}] with path = "/mnt/{tag}"'
      )
    path = conf.get("path")
    if not _nonempty_str(path):
      raise ValueError(f'host_volume.{tag}: missing required key "path"')
    readonly = conf.get("readonly", False)
    if not isinstance(readonly, bool):
      raise ValueError(f"host_volume.{tag}: readonly must be a boolean")
    volumes[tag] = HostVolume(tag=tag, path=ep(path), readonly=readonly)

  return volumes


def _parse_app_sources(entries, apps_root: Path, ep) -> dict[str, Path]:
  """The `[[app_source]]` blocks that add app directories beyond `apps/`.
  Names and locations must both be unique
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
    name = ""
    location = ""
    if isinstance(entry, dict):
      name = entry.get("name", "")
      location = entry.get("location", "")
    if not _nonempty_str(name) or not _nonempty_str(location):
      return refuse(
        'each [[app_source]] needs a name and a location, e.g. name = "dev", '
        'location = "~/code/happs"'
      )

    try:
      validate_identifier(name)
    except ValueError as e:
      return refuse(f"app_source name {name!r} is not a valid name: {e}")

    if name == DEFAULT_APP_SOURCE or name in sources:
      return refuse(
        f"app_source {name!r} is defined twice (or collides with the built-in "
        f"{DEFAULT_APP_SOURCE!r} source at {apps_root}); give it another name"
      )

    path = ep(location)
    if path in names_by_path:
      return refuse(
        f"app_source {name!r} points at {path}, which is already the "
        f"{names_by_path[path]!r} source; every app there would resolve twice"
      )

    names_by_path[path] = name
    sources[name] = path

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
