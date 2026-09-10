from kelso.jobs.job import Job, logger
from kelso.lib.kelso import KelsoCtx
from kelso.lib.metric import record_host_stats, record_volume_sizes


class VolumeMetricsJob(Job):
  name = "volume-metrics"
  description = "Record volume and kelso directory sizes"
  record_activity = False

  def run(self, ctx: KelsoCtx) -> None:
    n = record_volume_sizes(ctx)
    logger.info("Recorded %d volume size gauges", n)


class HostMetricsJob(Job):
  name = "host-metrics"
  description = "Record host and running-app resource gauges"
  record_activity = False

  def run(self, ctx: KelsoCtx) -> None:
    n = record_host_stats(ctx)
    logger.info("Recorded %d host gauges", n)
