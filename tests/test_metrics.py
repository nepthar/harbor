"""Volume-size and host-resource metric jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from harbor.jobs.metrics import HostMetricsJob, VolumeMetricsJob
from harbor.jobs.runner import metric_schedule
from harbor.lib import activity
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.metric import (
  _pct,
  cpu_used_ratio,
  drive_used_ratio,
  mem_used_ratio,
  mounted_disks,
  record_host_stats,
  record_volume_sizes,
  swap_used_ratio,
)


def _ctx(harbor_env) -> HarborCtx:
  cfg = load_config()
  assert cfg is not None
  return HarborCtx(cfg)


def test_volume_metrics_records_each_directory(harbor_env):
  data = harbor_env.volumes_root / "data" / "demo.app" / "uploads"
  data.mkdir(parents=True)
  (data / "blob").write_bytes(b"x" * 100)
  leftover = harbor_env.volumes_root / "temp" / "gone.app" / "scratch"
  leftover.mkdir(parents=True)
  (leftover / "tmp").write_bytes(b"y" * 40)
  media = harbor_env.root / "external-data"
  media.mkdir()
  (media / "clip").write_bytes(b"z" * 20)
  snapshots = harbor_env.root / "snapshots"
  snapshots.mkdir()
  (snapshots / "blob").write_bytes(b"s" * 50)

  ctx = _ctx(harbor_env)
  job = VolumeMetricsJob.call({}, ctx)
  assert job.state == "done"

  gauges = {k: int(e.value) for k, e in ctx.read_gauges("").items()}
  assert gauges["gauge/volume_size_bytes/demo.app/data/uploads"] == 100
  assert gauges["gauge/volume_size_bytes/gone.app/temp/scratch"] == 40
  assert gauges["gauge/volume_size_bytes//host/media"] == 20
  assert gauges["gauge/snapshots_size_bytes"] == 50
  assert gauges["gauge/var_size_bytes"] > 0
  assert activity.list_runs(ctx) == []
  assert job.log is None


def test_host_metrics_records_disk_and_app_stats(harbor_env, monkeypatch):
  harbor_env.set_containers(
    [
      {
        "app_id": "demo.app",
        "run_unit": "main",
        "id": "abc123",
        "state": "running",
        "cpu_perc": "25.00%",
        "mem_perc": "10.00%",
      }
    ]
  )
  monkeypatch.setattr("harbor.lib.metric.cpu_used_ratio", lambda: 0.4)
  monkeypatch.setattr("harbor.lib.metric.mem_used_ratio", lambda: 0.5)
  monkeypatch.setattr("harbor.lib.metric.swap_used_ratio", lambda: 0.1)
  monkeypatch.setattr("harbor.lib.metric.mounted_disks", lambda: [("root", Path("/"))])
  monkeypatch.setattr("harbor.lib.metric.drive_used_ratio", lambda _p: 0.25)

  ctx = _ctx(harbor_env)
  HostMetricsJob.call({}, ctx)

  gauges = {k: float(e.value) for k, e in ctx.read_gauges("").items()}
  assert gauges["gauge/host_cpu_used_ratio"] == 0.4
  assert gauges["gauge/host_mem_used_ratio"] == 0.5
  assert gauges["gauge/host_swap_used_ratio"] == 0.1
  assert gauges["gauge/host_drive_used_ratio/root"] == 0.25
  assert gauges["gauge/cpu_used_ratio/demo.app"] == 0.25
  assert gauges["gauge/mem_used_ratio/demo.app"] == 0.1
  assert activity.list_runs(ctx) == []


def test_metric_jobs_are_on_the_harbord_schedule():
  submitted: list[str] = []
  sched = metric_schedule(submitted.append)
  jobs = {j.job_func.args[0]: (j.interval, j.unit) for j in sched.get_jobs()}
  assert jobs["host-metrics"] == (5, "minutes")
  assert jobs["volume-metrics"] == (1, "hours")


def test_cpu_used_ratio_from_psutil(monkeypatch):
  monkeypatch.setattr(
    "harbor.lib.metric.psutil.cpu_percent", lambda interval=None: 40.0
  )
  assert cpu_used_ratio() == 0.4


def test_mem_used_ratio_from_available(monkeypatch):
  monkeypatch.setattr(
    "harbor.lib.metric.psutil.virtual_memory",
    lambda: SimpleNamespace(total=1000, available=250),
  )
  assert mem_used_ratio() == 0.75


def test_swap_used_ratio_skips_when_there_is_none(monkeypatch):
  monkeypatch.setattr(
    "harbor.lib.metric.psutil.swap_memory",
    lambda: SimpleNamespace(total=0, percent=0.0),
  )
  assert swap_used_ratio() is None


def test_mounted_disks_skips_pseudo_filesystems(monkeypatch):
  monkeypatch.setattr(
    "harbor.lib.metric.psutil.disk_partitions",
    lambda all=False: [
      SimpleNamespace(device="/dev/sda1", mountpoint="/", fstype="ext4"),
      SimpleNamespace(device="tmpfs", mountpoint="/run", fstype="tmpfs"),
    ],
  )
  assert mounted_disks() == [("sda1", Path("/"))]


def test_mounted_disks_falls_back_to_root_when_empty(monkeypatch):
  monkeypatch.setattr("harbor.lib.metric.psutil.disk_partitions", lambda all=False: [])
  assert mounted_disks() == [("root", Path("/"))]


def test_drive_used_ratio(monkeypatch):
  monkeypatch.setattr(
    "harbor.lib.metric.psutil.disk_usage",
    lambda _p: SimpleNamespace(total=100, used=25),
  )
  assert drive_used_ratio(Path("/")) == 0.25


def test_pct_parses_docker_percent():
  assert _pct("12.50%") == 0.125
  assert _pct("0.00%") == 0.0
  assert _pct(None) == 0.0
  assert _pct("nope") == 0.0


def test_record_host_stats_skips_unavailable_gauges(harbor_env, monkeypatch):
  monkeypatch.setattr("harbor.lib.metric.cpu_used_ratio", lambda: None)
  monkeypatch.setattr("harbor.lib.metric.mem_used_ratio", lambda: None)
  monkeypatch.setattr("harbor.lib.metric.swap_used_ratio", lambda: None)
  monkeypatch.setattr("harbor.lib.metric.mounted_disks", lambda: [("root", Path("/"))])
  monkeypatch.setattr("harbor.lib.metric.drive_used_ratio", lambda _p: 0.3)
  ctx = _ctx(harbor_env)
  n = record_host_stats(ctx)
  assert n == 1
  assert (
    ctx.read_gauges("host_drive_used_ratio/")["gauge/host_drive_used_ratio/root"].value
    == "0.3"
  )


def test_record_volume_sizes_skips_missing_host_paths(harbor_env):
  ctx = _ctx(harbor_env)
  n = record_volume_sizes(ctx)
  assert n == 1
  assert ctx.read_gauges("volume_size_bytes/") == {}
  assert "gauge/var_size_bytes" in ctx.read_gauges("var_size_bytes")
  assert ctx.read_gauges("snapshots_size_bytes") == {}


def test_history_gauges_keeps_points_from_since(harbor_env):
  ctx = _ctx(harbor_env)
  now = int(datetime.now(UTC).timestamp())
  old = (
    (datetime.now(UTC) - timedelta(hours=3))
    .isoformat(timespec="seconds")
    .replace("+00:00", "Z")
  )
  with ctx.config.metrics_log.open("a") as f:
    f.write(f"{old}\tset\tgauge/host_cpu_used_ratio\t0.9\n")
  ctx.record_gauge("host_cpu_used_ratio", 0.2)
  ctx.record_gauge("host_mem_used_ratio", 0.5)

  cpu = ctx.history_gauges("host_cpu_used_ratio", now - 3600)
  assert [float(e.value) for e in cpu["gauge/host_cpu_used_ratio"]] == [0.2]
  host = ctx.history_gauges("host_", now - 3600)
  assert set(host) == {
    "gauge/host_cpu_used_ratio",
    "gauge/host_mem_used_ratio",
  }
