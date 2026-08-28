from harbor.jobs.job import Job, logger
from harbor.lib.harbor import HarborCtx
from harbor.lib.metric import record_host_stats, record_volume_sizes


class VolumeMetricsJob(Job):
  name = "volume-metrics"
  description = "Record volume, var, and snapshot directory sizes"
  record_activity = False

  def run(self, ctx: HarborCtx) -> None:
    n = record_volume_sizes(ctx)
    logger.info("Recorded %d volume size gauges", n)


class HostMetricsJob(Job):
  name = "host-metrics"
  description = "Record host and running-app resource gauges"
  record_activity = False

  def run(self, ctx: HarborCtx) -> None:
    n = record_host_stats(ctx)
    logger.info("Recorded %d host gauges", n)
