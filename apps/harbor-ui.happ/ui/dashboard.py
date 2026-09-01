"""The dashboard: host CPU and memory, and every installed app."""

import json

from api import api, where
from installed import apps_table
from layout import esc

_CHART_JS = """
<script>
(function () {
  var el = document.getElementById("host-metrics");
  if (!el || typeof uPlot === "undefined") return;
  var payload = JSON.parse(el.textContent);
  draw("chart-cpu", payload.cpu, payload.since, payload.until);
  draw("chart-mem", payload.mem, payload.since, payload.until);

  function draw(id, points, since, until) {
    var mount = document.getElementById(id);
    if (!mount) return;
    if (!points.length) {
      mount.innerHTML = '<p class="muted">No samples in the last hour.</p>';
      return;
    }
    var xs = points.map(function (p) { return p.t; });
    var ys = points.map(function (p) { return p.v; });
    var plot = new uPlot({
      width: mount.clientWidth || 400,
      height: 220,
      cursor: { focus: { prox: 24 } },
      legend: { show: false },
      scales: {
        x: { time: true, auto: false, range: [since, until] },
        y: { auto: false, range: [0, 1] }
      },
      axes: [
        {
          stroke: "#bfb5a8",
          font: '11px "IBM Plex Mono", ui-monospace, monospace',
          grid: { stroke: "#232020", width: 1 },
          ticks: { stroke: "#2e2925" }
        },
        {
          stroke: "#bfb5a8",
          font: '11px "IBM Plex Mono", ui-monospace, monospace',
          grid: { stroke: "#232020", width: 1 },
          ticks: { stroke: "#2e2925" },
          values: function (u, splits) {
            return splits.map(function (v) { return Math.round(v * 100) + "%"; });
          }
        }
      ],
      series: [
        {},
        {
          stroke: "#fc795f",
          width: 2,
          fill: "rgba(252, 121, 95, 0.10)",
          points: { show: true, size: 5, fill: "#fc795f" }
        }
      ]
    }, [xs, ys], mount);
    new ResizeObserver(function () {
      plot.setSize({ width: mount.clientWidth || 400, height: 220 });
    }).observe(mount);
  }
})();
</script>
"""


def page(version):
  body = api("/metrics?prefix=host_&hours=1")
  metrics = body.get("metrics") or {}
  payload = json.dumps({
    "since": body["since"],
    "until": body["until"],
    "cpu": metrics.get("host_cpu_used_ratio") or [],
    "mem": metrics.get("host_mem_used_ratio") or [],
  })
  return (
    '<link rel="stylesheet" href="/static/uplot-1.6.32/uPlot.min.css">'
    f'<p class="lede">Connected to harbor {esc(version)} over '
    f"<code>{esc(where())}</code>.</p>"
    '<div class="charts">'
    '<div class="card pad"><h2>Host CPU</h2>'
    '<div class="chart" id="chart-cpu"></div></div>'
    '<div class="card pad"><h2>Host memory</h2>'
    '<div class="chart" id="chart-mem"></div></div>'
    "</div>"
    + "<h2>Installed apps</h2>"
    + apps_table(api("/apps").get("apps", []))
    + f'<script type="application/json" id="host-metrics">{payload}</script>'
    + '<script src="/static/uplot-1.6.32/uPlot.iife.min.js"></script>'
    + _CHART_JS
  )
