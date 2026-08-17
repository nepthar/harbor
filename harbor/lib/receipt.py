from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from harbor.lib.config import NONE_ROUTE_PROVIDER_TAG
from harbor.lib.harbor import HarborCtx
from harbor.lib.run_layout import AppRunData
from harbor.lib.stack import AppStack
from harbor.lib.util import PUBLIC_ROUTE_SCHEME

# Every label in these receipts pads to at least this, so the capability block
# and the location block under it line up in one `harbor start`.
LABEL_WIDTH = len("Containers:")


def published_route_urls(
  stack: AppStack, run_data: AppRunData, ctx: HarborCtx
) -> dict[str, str]:
  """Route name -> public URL, for routes assigned to a non-none provider."""
  assignments = ctx.app_store(stack.app).list_route_assignments()
  urls: dict[str, str] = {}
  for route_name, route in stack.routes.items():
    tag = assignments.get(route_name)
    if not tag or tag == NONE_ROUTE_PROVIDER_TAG:
      continue
    if route_name in run_data.route_urls:
      urls[route_name] = run_data.route_urls[route_name]
      continue
    if not stack.subdomain:
      continue
    domain = ctx.config.provider_domain(tag)
    urls[route_name] = (
      f"{PUBLIC_ROUTE_SCHEME}://{route.subdomain(stack.subdomain)}.{domain}"
    )
  return urls


def published_urls(stack: AppStack, run_data: AppRunData, ctx: HarborCtx) -> list[str]:
  """URLs for routes assigned to a non-none provider, in declaration order."""
  return list(published_route_urls(stack, run_data, ctx).values())


def route_lines(
  stack: AppStack,
  run_data: AppRunData | None,
  published: Mapping[str, str],
) -> list[str]:
  """Every declared route, read right to left: where you reach it, then what
  answers.

  `published` supplies the public URL for the routes that have one; a route
  missing from it simply stops at the host port. URLs are printed bare rather
  than wrapped in terminal hyperlink escapes -- that is what makes them
  clickable in a terminal *and* still useful piped into anything else.
  """
  lines: list[str] = []
  for name, route in stack.routes.items():
    assigned = run_data.routes.get(name) if run_data else None
    if assigned is not None and assigned.host_port > 0:
      where = f"http://localhost:{assigned.host_port}"
    elif stack.network_mode == "host":
      # Host networking maps nothing, so the container port is the host port
      # and harbor never allocated one.
      where = f"http://localhost:{route.container_port}"
    else:
      where = "(no host port allocated)"

    line = f"{route.run_unit_name}:{route.container_port}/{route.proto} <- {where}"
    if name in published:
      line += f" <- {published[name]}"
    lines.append(line)
  return lines


def config_lines(stack: AppStack, ctx: HarborCtx, *, installed: bool) -> list[str]:
  """Per-key config status, same wording as `harbor config`."""
  if not stack.config:
    return []
  store = None
  if installed and ctx.config.app_config_path(stack.app).is_file():
    store = ctx.app_store(stack.app)
  lines: list[str] = []
  for name, entry in stack.config.items():
    if store is None:
      if entry.secret:
        display = "(secret)"
      elif entry.has_default():
        display = f"{entry.default} (default)"
      else:
        display = "(required)"
    else:
      secret, value = store.get_config(name)
      if value is None:
        if entry.has_default():
          display = f"{entry.default} (default)"
        else:
          display = "(required)"
      elif secret:
        display = "(secret)"
      else:
        display = value
    lines.append(f"{name}: {display}")
  return lines


def volume_lines(
  stack: AppStack, run_data: AppRunData | None, ctx: HarborCtx
) -> list[str]:
  """Managed volume dirs and host-volume bind paths."""
  lines: list[str] = []
  app_id = stack.app
  for name, volume in stack.volumes.items():
    if run_data is not None and name in run_data.volume_links:
      lines.append(f"{name}: {run_data.volume_links[name].source}")
      continue
    if volume.kind == "host":
      lines.append(f"{name}: (unbound)")
    elif volume.kind == "app":
      continue
    else:
      root = ctx.config.volume_roots.get(volume.kind)
      if root is not None:
        lines.append(f"{name}: {root / app_id / name}")
  return lines


def location_receipt(
  stack: AppStack,
  run_data: AppRunData,
  ctx: HarborCtx,
  *,
  heading: str | None = None,
) -> str:
  """Post-up / status location block (Routes, Data, Logs)."""
  app_id = stack.app
  title = heading if heading is not None else f"Running {app_id}"
  rows: list[tuple[str, str]] = []

  routes = route_lines(stack, run_data, published_route_urls(stack, run_data, ctx))
  for i, line in enumerate(routes):
    rows.append(("Routes:" if i == 0 else "", line))

  vols = volume_lines(stack, run_data, ctx)
  data_line = next((line for line in vols if line.startswith("data:")), None)
  if data_line is not None:
    rows.append(("Data:", data_line.split(": ", 1)[1]))
  elif vols:
    rows.append(("Data:", vols[0].split(": ", 1)[1] if ": " in vols[0] else vols[0]))

  rows.append(("Logs:", f"harbor logs -f {app_id}"))

  return _format_labeled(title, rows)


def capability_receipt(
  stack: AppStack,
  run_data: AppRunData | None,
  ctx: HarborCtx,
  *,
  compact: bool = True,
  notes: tuple[str, ...] = (),
) -> str:
  """Manifest capability summary for inspect / first-run up."""
  app_id = stack.app
  lines: list[str] = [f"{app_id}"]

  containers = [f"{name}, image={unit.image}" for name, unit in stack.run_units.items()]
  if containers:
    lines.append(_labeled_line("Containers:", containers[0]))
    for extra in containers[1:]:
      lines.append(_labeled_line("", extra))

  if not compact:
    declared_ports: list[str] = []
    for unit_name, unit in stack.run_units.items():
      for port_name, port in unit.routes.items():
        if port.host_port < 0:
          spec = f"{port.container_port}/{port.proto} (allocated)"
        else:
          spec = f"{port.host_port}:{port.container_port}/{port.proto}"
        route = stack.routes.get(port_name)
        private = ", private" if route and route.private else ""
        declared_ports.append(f"{unit_name}.{port_name}: {spec}{private}")
    if declared_ports:
      lines.append(_labeled_line("Ports:", declared_ports[0]))
      for extra in declared_ports[1:]:
        lines.append(_labeled_line("", extra))

    if stack.subdomain and run_data is not None:
      pubs = published_urls(stack, run_data, ctx)
      if pubs:
        lines.append(_labeled_line("Routes:", pubs[0]))
        for extra in pubs[1:]:
          lines.append(_labeled_line("", extra))
    elif stack.subdomain:
      lines.append(_labeled_line("Routes:", f"subdomain={stack.subdomain}"))

    vols = volume_lines(stack, run_data, ctx)
    if vols:
      lines.append(_labeled_line("Volumes:", vols[0]))
      for extra in vols[1:]:
        lines.append(_labeled_line("", extra))

    configs = config_lines(stack, ctx, installed=run_data is not None)
    if configs:
      lines.append(_labeled_line("Config:", configs[0]))
      for extra in configs[1:]:
        lines.append(_labeled_line("", extra))

  dangers = danger_callouts(stack)
  for danger in dangers:
    lines.append(_labeled_line("Danger:", danger))

  for note in notes:
    lines.append(_labeled_line("Note:", note))

  if compact:
    # Drop the title line when embedding under Running …
    return "\n".join(lines[1:] if lines[0] == app_id else lines)

  return "\n".join(lines)


def danger_callouts(stack: AppStack) -> list[str]:
  callouts: list[str] = []
  if stack.network_mode == "host":
    callouts.append("host networking (no port isolation)")
  for name, volume in stack.volumes.items():
    if volume.kind == "host" and not volume.readonly:
      callouts.append(f"writable host bind '{name}'")
  return callouts


def status_receipt(
  stack: AppStack,
  run_data: AppRunData,
  ctx: HarborCtx,
  *,
  state_line: str,
  source: Path | str,
  last_action: str | None,
) -> str:
  """Full ``harbor status APP`` card."""
  app_id = stack.app
  rows: list[tuple[str, str]] = [
    ("State:", state_line),
    ("Source:", str(source)),
  ]

  routes = route_lines(stack, run_data, published_route_urls(stack, run_data, ctx))
  for i, line in enumerate(routes):
    rows.append(("Routes:" if i == 0 else "", line))

  if run_data.start_blockers:
    rows.append(("Config:", f"incomplete ({len(run_data.start_blockers)} issue(s))"))
  else:
    rows.append(("Config:", "complete"))

  vols = volume_lines(stack, run_data, ctx)
  if vols:
    rows.append(("Volumes:", vols[0]))
    for extra in vols[1:]:
      rows.append(("", extra))

  rows.append(("Last action:", last_action or "-"))
  rows.append(("Logs:", f"harbor logs -f {app_id}"))

  return _format_labeled(app_id, rows)


def _labeled_line(label: str, value: str) -> str:
  return f"  {label:<{LABEL_WIDTH}}  {value}"


def _format_labeled(title: str, rows: list[tuple[str, str]]) -> str:
  width = max((len(label) for label, _ in rows if label), default=0)
  width = max(width, LABEL_WIDTH)
  lines = [title]
  for label, value in rows:
    lines.append(f"  {label:<{width}}  {value}")
  return "\n".join(lines)


__all__ = [
  "capability_receipt",
  "config_lines",
  "danger_callouts",
  "location_receipt",
  "published_route_urls",
  "published_urls",
  "route_lines",
  "status_receipt",
  "volume_lines",
]
