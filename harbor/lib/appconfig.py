"""Per-app state: config values, volume binds, and staging metadata.

Lives at ``run/<app_id>/config.logtab`` rather than in the central harbordb so
that the run directory is the whole app -- see docs/run-layout.md L5. Anything
involving contention *between* apps (routes, host ports, system secrets) stays
in harbordb.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harbor.lib.crypto import CryptoEngine
from harbor.lib.logtab import LogTab
from harbor.lib.store import MAX_NAME_LEN, MAX_VALUE_LEN


def config_path(run_path: Path) -> Path:
  """Where an app's config lives, without creating it.

  Staging has to know whether the file is *absent* before anything touches it
  (docs/run-layout.md §5 step 5), and constructing the store creates it.
  """
  return run_path / "config.logtab"


class AppConfigStore:
  """Config values, binds, and metadata for a single staged app.

  Secret values are Fernet ciphertext under the master key; only
  :meth:`get_config` decrypts, so callers that merely need to know whether a
  value is set never handle plaintext.
  """

  def __init__(self, path: Path, crypto: CryptoEngine) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    self._table = LogTab(path, title="Harbor App Config")
    self._crypto = crypto

  def _write(self, key: str, value: Any) -> None:
    self._table.write(key, json.dumps(value, separators=(",", ":")))

  def _read(self, key: str) -> Any:
    raw = self._table.read(key)
    return json.loads(raw) if raw is not None else None

  def _section(self, prefix: str) -> dict[str, Any]:
    matches = self._table.scan(prefix=prefix)
    return {k.removeprefix(prefix): json.loads(v) for k, v in matches.items()}

  def set_config(self, name: str, secret: bool, value: str) -> None:
    if len(name) > MAX_NAME_LEN:
      raise ValueError(f"Name too long for config {name!r}")
    if len(value) > MAX_VALUE_LEN:
      raise ValueError(f"Value too long for config {name!r}")
    stored = self._crypto.encrypt(value) if secret else value
    self._write(f"config/{name}", {"secret": secret, "value": stored})

  def get_config(self, name: str) -> tuple[bool, str] | tuple[None, None]:
    """Return (secret, plaintext_value), or (None, None) if not set."""
    entry = self._read(f"config/{name}")
    if entry is None:
      return (None, None)
    secret = entry["secret"]
    raw = entry["value"]
    return secret, self._crypto.decrypt(raw) if secret else raw

  def has_config(self, name: str) -> bool:
    """Whether a value is stored, without decrypting it."""
    return self._read(f"config/{name}") is not None

  def set_bind(self, volume_name: str, host_path: str, readonly: bool = False) -> None:
    self._write(f"binds/{volume_name}", {"host_path": host_path, "readonly": readonly})

  def list_binds(self) -> dict[str, Any]:
    return self._section("binds/")

  def set_meta(self, name: str, value: str) -> None:
    self._write(f"meta/{name}", value)

  def get_meta(self, name: str) -> str | None:
    return self._read(f"meta/{name}")
