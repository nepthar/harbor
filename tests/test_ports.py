"""Host-port claims: next_free_port over AssignedRoute records in harbordb."""

from pathlib import Path

import pytest

from harbor.lib.crypto import NoopCryptoEngine
from harbor.lib.run_layout import AssignedRoute
from harbor.lib.store import HarborStore, JsonLogtabStore


def _route(
  name: str,
  host_port: int,
  *,
  container_port: int = 80,
) -> dict:
  return AssignedRoute(
    name=name,
    subdomain="",
    run_unit_name="main",
    host_port=host_port,
    container_port=container_port,
    proto="tcp",
    scheme="http",
  ).__dict__


@pytest.fixture
def db(tmp_path: Path) -> HarborStore:
  return HarborStore(
    JsonLogtabStore(tmp_path / "harbordb.logtab"), NoopCryptoEngine(), 41000
  )


def test_next_free_port_starts_at_base(db: HarborStore):
  assert db.next_free_port() == 41000


def test_next_free_port_skips_occupied(db: HarborStore):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db._store.write("routes/app-a/api", _route("api", 41001))
  assert db.next_free_port() == 41002


def test_list_app_routes_returns_records(db: HarborStore):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db._store.write("routes/app-a/api", _route("api", 41001))
  assert db.list_routes("app-a") == {
    "main": _route("main", 41000),
    "api": _route("api", 41001),
  }


def test_clear_routes_frees_ports(db: HarborStore):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db.clear_routes("app-a")
  assert db.list_routes("app-a") == {}
  assert db.next_free_port() == 41000


def test_purge_app_clears_routes(db: HarborStore):
  db._store.write("routes/app-a/main", _route("main", 41000))
  db._store.write("apps/app-a/config/x", {"secret": False, "value": "y"})
  assert db.purge_app("app-a") is True
  assert db.list_routes("app-a") == {}
  assert db.next_free_port() == 41000


def test_repo_state_round_trips_and_can_be_dropped(db: HarborStore):
  db.set_repo_state("harbor", sha="a" * 40, at="2026-01-01T00:00:00Z")

  assert db.get_repo_state("harbor") == {
    "sha": "a" * 40,
    "at": "2026-01-01T00:00:00Z",
  }
  assert db.get_repo_state("missing") is None

  db.del_repo_state("harbor")
  assert db.get_repo_state("harbor") is None


def test_pinned_port_outside_range_still_occupies_slot(db: HarborStore):
  db._store.write("routes/app-a/admin", _route("admin", 9000))
  # Harbor allocator still starts at port_base; pinned ports only matter when
  # they fall inside the scanned occupied set for that base.
  assert db.next_free_port() == 41000
  db._store.write("routes/app-b/main", _route("main", 41000))
  assert db.next_free_port() == 41001
