"""Where an app stands, and whether what is running is current."""

from __future__ import annotations

from harbor.lib.apps import AppID
from harbor.lib.docker import HarborRunUnitStatus
from harbor.lib.observations import AppObservation


def _observed(**kwargs) -> AppObservation:
  base = dict(
    app_id=AppID("demo.app"),
    bundle_path=None,
    run_dir_exists=True,
    compose_exists=True,
    config_exists=True,
    volumes_exist=True,
    containers=(
      HarborRunUnitStatus(
        app_id="demo.app",
        run_unit="main",
        container_id="abc",
        name="demo",
        state="running",
      ),
    ),
    db_present=True,
    last_action="started",
  )
  return AppObservation(**{**base, **kwargs})


def test_config_written_after_the_start_is_pending():
  assert _observed(
    started_at="2026-08-27T10:00:00Z", config_changed_at="2026-08-27T10:05:00Z"
  ).config_pending


def test_config_written_before_the_start_is_applied():
  assert not _observed(
    started_at="2026-08-27T10:05:00Z", config_changed_at="2026-08-27T10:00:00Z"
  ).config_pending


def test_nothing_is_pending_on_an_app_that_is_not_running():
  """The next start reads config fresh, so there is nothing to warn about."""
  assert not _observed(
    containers=(),
    started_at="2026-08-27T10:00:00Z",
    config_changed_at="2026-08-27T10:05:00Z",
  ).config_pending


def test_unknown_timestamps_are_not_pending():
  """A start the activity log has compacted away is unknown, not stale."""
  assert not _observed(
    started_at=None, config_changed_at="2026-08-27T10:05:00Z"
  ).config_pending
  assert not _observed(
    started_at="2026-08-27T10:00:00Z", config_changed_at=None
  ).config_pending
