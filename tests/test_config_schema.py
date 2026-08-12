"""Focused tests for the [config] manifest schema and store migration.

Covers the params -> config rename plus the new field schema
(kind -> secret bool) and store persistence (secret bool).

Self-contained: does not depend on the broader e2e suite.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harbor.lib.apps import AppID
from harbor.lib.crypto import FernetCryptoEngine, NoopCryptoEngine
from harbor.lib.manifest import ConfigEntry, parse_manifest
from harbor.lib.stack import HARBOR_CONFIG_ENV_PREFIX, AppConfig, AppStack
from harbor.lib.store import AppStore

FIXTURES = Path(__file__).parent / "fixtures" / "apps"


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
  return AppStack.from_bytes(
    manifest_bytes, AppID("io.test.example"), Path("manifest.toml")
  )


def test_app_stack_resolves_config():
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
    manifest = parse_manifest(path.read_bytes(), AppID(happ.split(".happ")[0]), path)
    assert manifest.config  # non-empty [config] section


# ── config.logtab round-trip (secret bool persistence) ────────────────────
def test_store_secret_round_trip_encrypts(tmp_path):
  path = tmp_path / "config.logtab"
  store = AppStore.from_path(path, FernetCryptoEngine("master-key"))
  store.set_config("admin_pass", secret=True, value="hunter2")

  secret, value = store.get_config("admin_pass")
  assert secret is True
  assert value == "hunter2"

  # At rest it is stored as {"secret": true, "value": <ciphertext>}, and the
  # plaintext appears nowhere in the file.
  assert "hunter2" not in path.read_text()


def test_store_plain_round_trip_is_plaintext(tmp_path):
  store = AppStore.from_path(tmp_path / "config.logtab", NoopCryptoEngine())
  store.set_config("admin_user", secret=False, value="alice")

  secret, value = store.get_config("admin_user")
  assert secret is False
  assert value == "alice"

  assert store.has_config("admin_user")
  assert not store.has_config("missing")
  assert store.get_config("missing") == (None, None)


def test_store_keeps_binds_and_meta(tmp_path):
  store = AppStore.from_path(tmp_path / "config.logtab", NoopCryptoEngine())
  store.set_bind("media", "nas_media")
  store.set_meta("origin", "/harbor/apps/io.test.example.happ")

  assert store.list_binds() == {"media": "nas_media"}
  assert store.get_meta("origin") == "/harbor/apps/io.test.example.happ"
  assert store.get_meta("staged_at") is None
