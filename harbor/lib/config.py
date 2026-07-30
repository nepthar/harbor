import logging
import os
import tomllib
from pathlib import Path

from harbor.lib.apps import AppID
from harbor.lib.logtab import LogTab

VOLUME_KINDS = ("data", "temp", "bulk", "logs")


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


class Config:
  harbor_root: Path
  volume_roots: dict[str, Path]
  apps_root: Path
  run_root: Path
  master_key: str
  master_keyfile: Path
  domain: str
  port_base: int
  route_provider: dict
  author: str

  def __init__(
    self,
    harbor_root: Path,
    volume_roots: dict[str, Path],
    apps_root: Path,
    run_root: Path,
    master_key: str,
    master_keyfile: Path,
    domain: str,
    author: str,
    port_base: int = 41000,
    route_provider: dict | None = None,
  ) -> None:
    self.harbor_root = harbor_root
    self.volume_roots = volume_roots
    self.apps_root = apps_root
    self.run_root = run_root
    self.master_key = master_key
    self.master_keyfile = master_keyfile
    self.domain = domain
    self.author = author
    self.port_base = port_base
    self.route_provider = route_provider or {}

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


def load_config_file(config_file: str | Path, author: str) -> Config:
  config_path = Path(config_file).resolve()
  config_dir = config_path.parent
  harbor_root = config_dir

  with open(config_path, "rb") as f:
    data = tomllib.load(f)

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
  master_key = LogTab(master_keyfile).read("master_key") or ""

  if master_key:
    logger.debug(f"Using master key from {master_keyfile}")
  else:
    logger.warning("Using empty master key")

  apps_root = ep(data.get("apps_root", "apps"))
  run_root = ep(data.get("run_root", "run"))
  domain = data.get("domain", "harbor.localhost")
  port_base = data.get("port_base", 41000)
  route_provider = data.get("route_provider", {})

  return Config(
    harbor_root=harbor_root,
    volume_roots=volume_roots,
    apps_root=apps_root,
    run_root=run_root,
    master_key=master_key,
    master_keyfile=master_keyfile,
    domain=domain,
    author=author,
    port_base=port_base,
    route_provider=route_provider,
  )


CONFIG_LOCATIONS = [
  Path("~/.harbor/config.toml"),
  Path("/etc/harbor/config.toml"),
]


def load_config(
  author: str,
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
    return load_config_file(path, author)

  if root is not None:
    path = (Path(root).expanduser() / "config.toml").resolve()
    if not path.is_file():
      raise RuntimeError(f"No config file exists at {path}")
    logger.debug(f"Loading from {path}")
    return load_config_file(path, author)

  if os.environ.get("HARBOR_CONFIG"):
    path = Path(os.environ["HARBOR_CONFIG"]).expanduser().resolve()
    if not path.is_file():
      raise RuntimeError(
        f"HARBOR_CONFIG is set to {path}, but no config file exists there"
      )
    logger.debug(f"Loading from {path}")
    return load_config_file(path, author)

  if os.environ.get("HARBOR_ROOT"):
    path = (Path(os.environ["HARBOR_ROOT"]).expanduser() / "config.toml").resolve()
    if not path.is_file():
      raise RuntimeError(f"HARBOR_ROOT is set, but no config file exists at {path}")
    logger.debug(f"Loading from {path}")
    return load_config_file(path, author)

  for candidate in CONFIG_LOCATIONS:
    path = candidate.expanduser().resolve()
    if path.is_file():
      logger.debug(f"Loading from {path}")
      return load_config_file(path, author)

  searched = ", ".join(str(c.expanduser().resolve()) for c in CONFIG_LOCATIONS)
  logger.warning(f"No harbor config found. Searched: {searched}")
  return None
