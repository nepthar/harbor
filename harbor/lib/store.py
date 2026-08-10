import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from harbor.lib.config import Config

from .crypto import CryptoEngine, crypto_from_config
from .logtab import LogTab

logger = logging.getLogger("harbor.store")

# These constants have been chosen arbitrarily and can be adjusted if use cases
# come up that require more.
MAX_NAME_LEN = 256
MAX_VALUE_LEN = 512
PORT_RANGE_SIZE = 1000


class ConfigStore(Protocol):
  """A flat string-keyed configuration store, suitable for lightweight state storage"""

  def write(self, key: str, value: Any) -> None: ...

  def read(self, key: str) -> Any: ...

  def scan(self, prefix: str = "", suffix: str = "") -> dict[str, Any]: ...

  def clear(self, prefix_or_key: str) -> None: ...

  def delete(self, key: str) -> None: ...


class JsonLogtabStore(ConfigStore):
  """Json-based ConfigStore on top of a Logtab"""

  def __init__(self, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    self._table = LogTab(path)

  def write(self, key: str, value: Any) -> None:
    self._table.write(key, json.dumps(value, separators=(",", ":")))

  def read(self, key: str) -> Any:
    entry = self._table.read(key)
    return json.loads(entry.value) if entry is not None else None

  def scan(self, prefix: str = "", suffix: str = "") -> dict[str, Any]:
    matches = self._table.scan(prefix=prefix, suffix=suffix)
    return {k.removeprefix(prefix): json.loads(v.value) for k, v in matches.items()}

  def clear(self, prefix_or_key: str) -> None:
    if not prefix_or_key.endswith("/"):
      raise ValueError("Prefix for clear should end with /")
    self._table.clear(prefix_or_key)

  def delete(self, key: str) -> None:
    self._table.delete(key)


class HarborStore:
  """Store and manipulate harbor-wide state"""

  @classmethod
  def from_config(cls, config: Config) -> "HarborStore":
    json_store = JsonLogtabStore(config.harbordb_path)
    crypto = crypto_from_config(config)
    return cls(json_store, crypto, config.port_base)

  def __init__(self, store: ConfigStore, crypto: CryptoEngine, port_base: int):
    self._store = store
    self._crypto = crypto
    self._port_base = port_base

  # Routes - the only per-app state in this DB. Everything else about an app
  # lives in its run directory; routes stay central because allocating a host
  # port is contention between apps, not state belonging to one.
  def list_routes(self, app_id: str) -> dict[str, dict[str, Any]]:
    return self._store.scan(f"routes/{app_id}/")

  def set_route(self, app_id: str, route_name: str, entry: Mapping[str, Any]) -> None:
    self._store.write(f"routes/{app_id}/{route_name}", dict(entry))

  def clear_routes(self, app_id: str) -> None:
    self._store.clear(f"routes/{app_id}/")

  def next_free_port(self) -> int:
    rout_entries = self._store.scan(prefix="routes/")
    occupied = set(int(entry["host_port"]) for entry in rout_entries.values())
    port = self._port_base
    max_port = self._port_base + PORT_RANGE_SIZE
    while port in occupied:
      port += 1
    if port > max_port:
      raise ValueError(f"No free host ports remaining. Limit is {PORT_RANGE_SIZE}")
    return port

  # Tokens - Ephemeral secrets with expiration, and automatic refresh (session keys, etc.)
  def set_token(self, key: str, value: str, expire_at_epoch_s: int = -1) -> None:
    self._store.write(
      f"system/tokens/{key}",
      {"tok": self._crypto.encrypt(value), "exp": expire_at_epoch_s},
    )

  def get_token(self, key: str) -> tuple[str, int] | tuple[None, None]:
    k = f"system/tokens/{key}"
    found = self._store.read(k)

    if not found:
      return None, None

    exp = found["exp"]
    if exp > 0 and int(time.time()) > exp:
      return None, None

    return self._crypto.decrypt(found["tok"]), exp

  # Secrets - Long-lived secrets like API keys or passwords
  def set_secret(self, name: str, value: str) -> None:
    if len(name) > MAX_NAME_LEN:
      raise ValueError(f"Name too long for secret {name!r}")
    if len(value) > MAX_VALUE_LEN:
      raise ValueError(f"Value too long for secret {name!r}")
    self._store.write(f"system/secrets/{name}", self._crypto.encrypt(value))

  def get_secret(self, name: str) -> str | None:
    raw = self._store.read(f"system/secrets/{name}")
    return self._crypto.decrypt(raw) if raw is not None else None

  def list_secrets(self) -> list[str]:
    secrets = self._store.scan("system/secrets/")
    return sorted(secrets.keys())

  def del_secret(self, name: str) -> None:
    self._store.delete(f"system/secrets/{name}")

  # App Management
  def app_ids(self) -> list[str]:
    """Every app harbordb still holds state for -- which now means routes.

    Config and binds moved to the run directory, so an id here with no run
    directory is an orphaned route allocation, and that is what `doctor`
    reports.
    """
    return sorted({key.split("/")[0] for key in self._store.scan("routes/")})

  def purge_app(self, app_id: str) -> bool:
    had = bool(self._store.scan(f"routes/{app_id}/"))
    self.clear_routes(app_id)
    return had


class AppStore:
  """Store and manipulate per-app state"""

  @classmethod
  def from_path(cls, path: Path, crypto: CryptoEngine) -> "AppStore":
    json_store = JsonLogtabStore(path)
    return cls(json_store, crypto)

  def __init__(self, store: ConfigStore, crypto: CryptoEngine) -> None:
    self._store = store
    self._crypto = crypto

  def set_config(self, name: str, secret: bool, value: str) -> None:
    if len(name) > MAX_NAME_LEN:
      raise ValueError(f"Name too long for config {name!r}")
    if len(value) > MAX_VALUE_LEN:
      raise ValueError(f"Value too long for config {name!r}")
    stored = self._crypto.encrypt(value) if secret else value
    self._store.write(f"config/{name}", {"secret": secret, "value": stored})

  def get_config(self, name: str) -> tuple[bool, str] | tuple[None, None]:
    """Return (secret, plaintext_value), or (None, None) if not set."""
    entry = self._store.read(f"config/{name}")
    if entry is None:
      return (None, None)
    secret = entry["secret"]
    raw = entry["value"]
    return secret, self._crypto.decrypt(raw) if secret else raw

  def has_config(self, name: str) -> bool:
    """Whether a value is stored, without decrypting it."""
    return self._store.read(f"config/{name}") is not None

  def set_bind(self, volume_name: str, host_path: str, readonly: bool = False) -> None:
    self._store.write(
      f"binds/{volume_name}", {"host_path": host_path, "readonly": readonly}
    )

  def list_binds(self) -> dict[str, Any]:
    return self._store.scan("binds/")

  def set_route_assignment(self, route_name: str, provider_tag: str) -> None:
    """Record which route-provider tag publishes ``route_name``."""
    self._store.write(f"routes/{route_name}", provider_tag)

  def get_route_assignment(self, route_name: str) -> str | None:
    """Return the assigned provider tag, or None if never set."""
    value = self._store.read(f"routes/{route_name}")
    return value if isinstance(value, str) else None

  def has_route_assignment(self, route_name: str) -> bool:
    return self._store.read(f"routes/{route_name}") is not None

  def list_route_assignments(self) -> dict[str, str]:
    """route name -> provider tag for every assignment on file."""
    raw = self._store.scan("routes/")
    return {name: tag for name, tag in raw.items() if isinstance(tag, str)}

  def set_meta(self, name: str, value: Any) -> None:
    self._store.write(f"meta/{name}", value)

  def get_meta(self, name: str) -> Any:
    return self._store.read(f"meta/{name}")
