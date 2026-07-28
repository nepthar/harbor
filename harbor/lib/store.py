"""Per-app state: config values, volume binds, and host-port claims."""

import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from harbor.lib.config import Config

from .crypto import CryptoEngine
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


class JsonConfigStore(ConfigStore):
  def __init__(self, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    self._table = LogTab(path)
  
  def write(self, key: str, value: Any) -> None:
    self._table.write(key, json.dumps(value, separators=(",", ":")))

  def read(self, key: str) -> Any:
    value = self._table.read(key)
    return json.loads(value) if value is not None else None

  def scan(self, prefix: str = "", suffix: str = "") -> dict[str, Any]:
    matches = self._table.scan(prefix=prefix, suffix=suffix)
    return {k.removeprefix(prefix): json.loads(v) for k, v in matches.items()}

  def clear(self, prefix_or_key: str) -> None:
    if not prefix_or_key.endswith("/"):
      raise ValueError("Prefix for clear should end with /")
    self._table.clear(prefix_or_key)

  def delete(self, key: str) -> None:
    self._table.delete(key)


class HarborDB:
  @classmethod
  def from_config(cls, config: Config) -> "HarborDB":
    json_store = JsonConfigStore(config.harbordb_path)
    crypto = CryptoEngine.from_config(config)
    return cls(json_store, crypto, config.port_base)

  def __init__(self, store: ConfigStore, crypto: CryptoEngine, port_base: int):
    self._store = store
    self._crypto = crypto
    self._port_base = port_base

  def app_db(self, app_id: str) -> "AppDB":
    return AppDB(self._store, app_id, self._crypto)

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
    app_ids = set()

    for k in self._store.scan("apps/").keys():
      app_ids.add(k.split("/")[0])

    return sorted(app_ids)

  def purge_app(self, app_id: str) -> bool:
    had = bool(self._store.scan(f"apps/{app_id}/")) or bool(
      self._store.scan(f"routes/{app_id}/")
    )
    self._store.clear(f"apps/{app_id}/")
    self._store.clear(f"routes/{app_id}/")
    return had


class AppDB:
  def __init__(
    self, harbor_store: ConfigStore, app_id: str, crypto: CryptoEngine
  ) -> None:
    self._store = harbor_store
    self._app_id = app_id
    self._crypto = crypto
    self._prefix = f"apps/{app_id}"

  @property
  def app_id(self) -> str:
    return self._app_id

  def _k(self, section: str, key: str) -> str:
    return f"{self._prefix}/{section}/{key}"

  def _data(self, section: str):
    return self._store.scan(f"{self._prefix}/{section}/")

  def set_config(self, name: str, secret: bool, value: str) -> None:
    if len(name) > MAX_NAME_LEN:
      raise ValueError(f"Name too long for config {name!r}")
    if len(value) > MAX_VALUE_LEN:
      raise ValueError(f"Value too long for config {name!r}")
    stored = self._crypto.encrypt(value) if secret else value
    key = self._k("config", name)
    self._store.write(key, {"secret": secret, "value": stored})

  def get_config(self, name: str) -> tuple[bool, str] | tuple[None, None]:
    """Return (secret, plaintext_value) or None if not set."""
    entry = self._store.read(self._k("config", name))
    if entry is None:
      return (None, None)
    secret = entry["secret"]
    raw = entry["value"]
    return secret, self._crypto.decrypt(raw) if secret else raw

  def set_bind(self, volume_name: str, host_path: str, readonly: bool = False) -> None:
    self._store.write(
      self._k("binds", volume_name), {"host_path": host_path, "readonly": readonly}
    )

  def list_binds(self) -> dict:
    return self._data("binds") or {}

  def list_routes(self) -> dict[str, dict[str, Any]]:
    return self._store.scan(f"routes/{self._app_id}/")

  def clear_routes(self) -> None:
    self._store.clear(f"routes/{self._app_id}/")
