"""Focused tests for the [config] manifest schema and store migration.

Covers the params -> config rename plus the new field schema
(kind -> secret bool) and store persistence (secret bool).

Self-contained: does not depend on the broader e2e suite.
"""

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harbor.lib.apps import AppID
from harbor.lib.crypto import FernetCryptoEngine, NoopCryptoEngine
from harbor.lib.manifest import ConfigEntry, parse_manifest_bytes
from harbor.lib.stack import HARBOR_CONFIG_ENV_PREFIX, AppConfig, build_app_stack
from harbor.lib.store import AppDB

FIXTURES = Path(__file__).parent / "fixtures" / "apps"


class FakeStore:
  """In-memory ConfigStore, isolating store tests from the on-disk LogTab."""

  def __init__(self) -> None:
    self._data: dict[str, Any] = {}

  def lock(self):
    return nullcontext()

  def write(self, key: str, value: Any) -> None:
    self._data[key] = value

  def read(self, key: str) -> Any:
    return self._data.get(key)

  def scan(self, prefix: str = "", suffix: str = "") -> dict[str, Any]:
    return {
      k.removeprefix(prefix): v
      for k, v in self._data.items()
      if k.startswith(prefix) and k.endswith(suffix)
    }

  def clear(self, prefix_or_key: str) -> None:
    for k in [k for k in self._data if k.startswith(prefix_or_key)]:
      del self._data[k]

  def delete(self, key: str) -> None:
    self._data.pop(key, None)


# ── ConfigEntry schema ────────────────────────────────────────────────────
def test_empty_config_entry_uses_all_defaults():
  entry = ConfigEntry.model_validate({})
  assert entry.desc == ""
  assert entry.default is None
  assert entry.secret is False


def test_secret_with_default():
  entry = ConfigEntry.model_validate({"secret": True, "default": "auto"})
  assert entry.secret is True
  assert entry.default == "auto"


def test_plain_with_default_and_desc():
  entry = ConfigEntry.model_validate({"default": "photos", "desc": "the subdomain"})
  assert entry.secret is False
  assert entry.default == "photos"
  assert entry.desc == "the subdomain"


def test_unknown_key_rejected():
  with pytest.raises(ValidationError):
    ConfigEntry.model_validate({"bogus": 1})


def test_legacy_kind_key_rejected():
  # The old `kind = "str"` form is no longer valid.
  with pytest.raises(ValidationError):
    ConfigEntry.model_validate({"kind": "str"})


# ── Manifest parsing + stack resolution ───────────────────────────────────
MANIFEST = b"""
[app]
version = "0.1.0"

[config]
admin_pass = { secret = true, default = "auto" }
admin_user = {}
subdomain  = { default = "photos" }

[run.main]
image = "alpine:latest"
env   = { ADMIN_USER = "${admin_user}" }
"""


def _stack_from(manifest_bytes):
  manifest = parse_manifest_bytes(manifest_bytes, Path("manifest.toml"))
  manifest._app_handle = AppID("io.test.example")
  return build_app_stack(manifest)


def test_build_app_stack_resolves_config():
  stack = _stack_from(MANIFEST)

  secret = stack.config["admin_pass"]
  required = stack.config["admin_user"]
  plain = stack.config["subdomain"]

  assert isinstance(secret, AppConfig)
  assert secret.secret is True and secret.default == "auto"
  # secret-with-default has no usable default (generated at stage time)
  assert secret.has_default() is False

  assert required.secret is False and required.default is None
  assert required.has_default() is False

  assert plain.secret is False and plain.default == "photos"
  assert plain.has_default() is True

  # env var substitution wiring uses the config prefix
  assert plain.env_name() == f"{HARBOR_CONFIG_ENV_PREFIX}_subdomain"


def test_config_env_var_injected_into_run_env():
  stack = _stack_from(MANIFEST)
  env = stack.run_units["main"].environment
  assert env["ADMIN_USER"] == f"${{{HARBOR_CONFIG_ENV_PREFIX}_admin_user}}"


def test_fixtures_parse_with_config_section():
  for happ in ("io.p2net.basic-features.happ", "routes-demo.happ"):
    path = FIXTURES / happ / "manifest.toml"
    manifest = parse_manifest_bytes(path.read_bytes(), path)
    assert manifest.config  # non-empty [config] section


# ── Store round-trip (secret bool persistence) ────────────────────────────
def _app_db(crypto):
  return AppDB(FakeStore(), "io.test.example", crypto)


def test_store_secret_round_trip_encrypts():
  store = FakeStore()
  db = AppDB(store, "io.test.example", FernetCryptoEngine("master-key"))
  db.set_config("admin_pass", secret=True, value="hunter2")

  secret, value = db.get_config("admin_pass")
  assert secret is True
  assert value == "hunter2"

  # At rest it is stored as {"secret": true, "value": <ciphertext>}
  raw = store.read("apps/io.test.example/config/admin_pass")
  assert raw["secret"] is True
  assert raw["value"] != "hunter2"
  assert "hunter2" not in raw["value"]


def test_store_plain_round_trip_is_plaintext():
  db = _app_db(NoopCryptoEngine())
  db.set_config("admin_user", secret=False, value="alice")

  secret, value = db.get_config("admin_user")
  assert secret is False
  assert value == "alice"

  assert db.get_config("missing") == (None, None)
