"""Volume-size and host-resource metric jobs."""

from __future__ import annotations

from pathlib import Path

from harbor.jobs.metrics import HostMetricsJob, VolumeMetricsJob
from harbor.jobs.runner import metric_schedule
from harbor.lib import activity
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.metric import (
  _cpu_times,
  _parse_meminfo,
  _pct,
  mounted_disks,
  record_host_stats,
  record_volume_sizes,
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

  ctx = _ctx(harbor_env)
  job = VolumeMetricsJob.call({}, ctx)
  assert job.state == "done"

  gauges = {k: int(e.value) for k, e in ctx.read_gauges("volume_size_bytes/").items()}
  assert gauges["gauge/volume_size_bytes/demo.app/data/uploads"] == 100
  assert gauges["gauge/volume_size_bytes/gone.app/temp/scratch"] == 40
  assert gauges["gauge/volume_size_bytes//host/media"] == 20
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


def test_cpu_times_from_proc_stat():
  text = "cpu  100 20 30 400 50 1 2 3 0 0\ncpu0 50 10 15 200 25 0 1 1 0 0\n"
  idle, total = _cpu_times(text)
  assert idle == 400 + 50
  assert total == 100 + 20 + 30 + 400 + 50 + 1 + 2 + 3


def test_parse_meminfo():
  info = _parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 0 kB\n")
  assert info["MemTotal"] == 1000
  assert info["MemAvailable"] == 250
  assert info["SwapTotal"] == 0


def test_pct_parses_docker_percent():
  assert _pct("12.50%") == 0.125
  assert _pct("0.00%") == 0.0
  assert _pct(None) == 0.0
  assert _pct("nope") == 0.0


def test_mounted_disks_falls_back_to_root_without_proc():
  disks = mounted_disks()
  assert disks
  assert all(isinstance(name, str) and mount.is_absolute() for name, mount in disks)


def test_record_host_stats_skips_missing_proc(harbor_env, monkeypatch):
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
  assert n == 0
  assert ctx.read_gauges("volume_size_bytes/") == {}
