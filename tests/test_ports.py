"""Host-port claims: next_free_port over AssignedRoute records in harbordb."""

from pathlib import Path

import pytest

from harbor.lib.crypto import NoopCryptoEngine
from harbor.lib.run_layout import AssignedRoute
from harbor.lib.store import HarborDB, JsonConfigStore


def _route(
  name: str,
  host_port: int,
  *,
  publish: str = "lan",
  container_port: int = 80,
) -> dict:
  return AssignedRoute(
    name=name,
    subdomain="",
    run_unit_name="main",
    host_port=host_port,
    container_port=container_port,
    proto="tcp",
    publish=publish,  # type: ignore[arg-type]
    scheme="http",
  ).__dict__


@pytest.fixture
def db(tmp_path: Path) -> HarborDB:
  return HarborDB(
    JsonConfigStore(tmp_path / "harbordb.logtab"), NoopCryptoEngine(), 41000
  )


def test_next_free_port_starts_at_base(db: HarborDB):
  assert db.next_free_port() == 41000


def test_next_free_port_skips_occupied(db: HarborDB):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db._store.write("routes/app-a/api", _route("api", 41001))
  assert db.next_free_port() == 41002


def test_list_app_routes_returns_records(db: HarborDB):
  db._store.write("routes/app-a/main", _route("main", 41000, publish="web"))
  db._store.write("routes/app-a/api", _route("api", 41001))
  assert db.app_db("app-a").list_routes() == {
    "main": _route("main", 41000, publish="web"),
    "api": _route("api", 41001),
  }


def test_clear_routes_frees_ports(db: HarborDB):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db.app_db("app-a").clear_routes()
  assert db.app_db("app-a").list_routes() == {}
  assert db.next_free_port() == 41000


def test_purge_app_clears_routes(db: HarborDB):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db._store.write("apps/app-a/config/x", {"secret": False, "value": "y"})
  assert db.purge_app("app-a") is True
  assert db.app_db("app-a").list_routes() == {}
  assert db.next_free_port() == 41000


def test_pinned_port_outside_range_still_occupies_slot(db: HarborDB):
  db._store.write("routes/app-a/admin", _route("admin", 9000))
  # Harbor allocator still starts at port_base; pinned ports only matter when
  # they fall inside the scanned occupied set for that base.
  assert db.next_free_port() == 41000
  db._store.write("routes/app-b/main", _route("main", 41000))
  assert db.next_free_port() == 41001
