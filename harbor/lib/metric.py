"""Volume-size and host/app resource gauges, written through `record_gauge`."""

from __future__ import annotations

from pathlib import Path

import psutil

from harbor.lib.docker import docker_run_command, load_harbor_run_unit_status
from harbor.lib.harbor import HarborCtx
from harbor.lib.util import path_size

CPU_SAMPLE_S = 0.2

_SKIP_FS = frozenset(
  {
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devfs",
    "devtmpfs",
    "fuse",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "rpc_pipefs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
  }
)


def record_volume_sizes(ctx: HarborCtx) -> int:
  """Walk volumes, host volumes, and the harbor directories. Returns how many."""
  n = 0
  for kind, root in ctx.config.volume_roots.items():
    if not root.is_dir():
      continue
    for app_dir in root.iterdir():
      if not app_dir.is_dir():
        continue
      for volume_dir in app_dir.iterdir():
        if not volume_dir.is_dir():
          continue
        ctx.record_gauge(
          f"volume_size_bytes/{app_dir.name}/{kind}/{volume_dir.name}",
          path_size(volume_dir),
        )
        n += 1
  for tag, volume in ctx.config.host_volumes.items():
    if not volume.path.exists():
      continue
    ctx.record_gauge(f"volume_size_bytes//host/{tag}", path_size(volume.path))
    n += 1
  for name, root in (
    ("var", ctx.config.var_root),
    ("snapshots", ctx.config.snapshot_root),
    ("repos", ctx.config.repos_root),
  ):
    if not root.exists():
      continue
    ctx.record_gauge(f"{name}_size_bytes", path_size(root))
    n += 1
  return n


def record_host_stats(ctx: HarborCtx) -> int:
  """CPU, memory, disks, and per-running-app docker stats. Returns how many."""
  n = 0
  cpu = cpu_used_ratio()
  if cpu is not None:
    ctx.record_gauge("host_cpu_used_ratio", cpu)
    n += 1
  mem = mem_used_ratio()
  if mem is not None:
    ctx.record_gauge("host_mem_used_ratio", mem)
    n += 1
  swap = swap_used_ratio()
  if swap is not None:
    ctx.record_gauge("host_swap_used_ratio", swap)
    n += 1
  for device, mount in mounted_disks():
    ratio = drive_used_ratio(mount)
    if ratio is None:
      continue
    ctx.record_gauge(f"host_drive_used_ratio/{device}", ratio)
    n += 1
  return n + record_app_stats(ctx)


def cpu_used_ratio(*, sample_s: float = CPU_SAMPLE_S) -> float:
  return psutil.cpu_percent(interval=sample_s) / 100.0


def mem_used_ratio() -> float | None:
  vm = psutil.virtual_memory()
  if vm.total <= 0:
    return None
  return max(0.0, min(1.0, 1 - vm.available / vm.total))


def swap_used_ratio() -> float | None:
  swap = psutil.swap_memory()
  if swap.total <= 0:
    return None
  return max(0.0, min(1.0, swap.percent / 100.0))


def mounted_disks() -> list[tuple[str, Path]]:
  """`(device_name, mountpoint)` for real disks, or `/` as `root`."""
  seen: dict[str, Path] = {}
  for part in psutil.disk_partitions(all=False):
    if part.fstype in _SKIP_FS:
      continue
    name = Path(part.device).name or "root"
    if name not in seen:
      seen[name] = Path(part.mountpoint)
  return list(seen.items()) or [("root", Path("/"))]


def drive_used_ratio(mount: Path) -> float | None:
  try:
    usage = psutil.disk_usage(str(mount))
  except OSError:
    return None
  if usage.total <= 0:
    return None
  return usage.used / usage.total


def record_app_stats(ctx: HarborCtx) -> int:
  """Per-running-app CPU and memory from `docker stats`. Returns how many."""
  by_id = _container_stats()
  if not by_id:
    return 0
  n = 0
  for app_id, units in load_harbor_run_unit_status().items():
    cpus: list[float] = []
    mems: list[float] = []
    for unit in units:
      if unit.state.lower() != "running":
        continue
      pair = _stats_for(unit.container_id, by_id)
      if pair is None:
        continue
      cpus.append(pair[0])
      mems.append(pair[1])
    if not cpus:
      continue
    ctx.record_gauge(f"cpu_used_ratio/{app_id}", sum(cpus))
    ctx.record_gauge(f"mem_used_ratio/{app_id}", sum(mems) / len(mems))
    n += 2
  return n


def _container_stats() -> dict[str, tuple[float, float]]:
  result = docker_run_command(["stats", "--no-stream"], check=False)
  if result.returncode != 0:
    return {}
  out: dict[str, tuple[float, float]] = {}
  for row in result.data:
    cid = row.get("ID") or row.get("Container") or ""
    if not cid:
      continue
    out[cid] = (_pct(row.get("CPUPerc")), _pct(row.get("MemPerc")))
  return out


def _stats_for(
  container_id: str, by_id: dict[str, tuple[float, float]]
) -> tuple[float, float] | None:
  if container_id in by_id:
    return by_id[container_id]
  for cid, pair in by_id.items():
    if container_id.startswith(cid) or cid.startswith(container_id):
      return pair
  return None


def _pct(raw: str | None) -> float:
  if not raw:
    return 0.0
  try:
    return float(raw.strip().rstrip("%")) / 100.0
  except ValueError:
    return 0.0
