from __future__ import annotations

from pathlib import Path

from harbor.lib.config import NONE_ROUTE_PROVIDER_TAG
from harbor.lib.harbor import HarborCtx
from harbor.lib.run_layout import AppRunData
from harbor.lib.stack import AppStack


def published_urls(stack: AppStack, run_data: AppRunData, ctx: HarborCtx) -> list[str]:
  """URLs for routes assigned to a non-none provider, in declaration order."""
  assignments = ctx.app_store(stack.app).list_route_assignments()
  urls: list[str] = []
  for route_name, route in stack.routes.items():
    tag = assignments.get(route_name)
    if not tag or tag == NONE_ROUTE_PROVIDER_TAG:
      continue
    if route_name in run_data.route_urls:
      urls.append(run_data.route_urls[route_name])
      continue
    if not stack.subdomain:
      continue
    domain = ctx.config.provider_domain(tag)
    urls.append(f"{route.scheme}://{route.subdomain(stack.subdomain)}.{domain}")
  return urls


def host_port_lines(stack: AppStack, run_data: AppRunData | None) -> list[str]:
  """Host port mappings as ``:<host> → <unit>:<container>/<proto>``."""
  lines: list[str] = []

  if run_data:
    for assigned_route in run_data.routes.values():
      host_port = (
        "<auto>" if assigned_route.host_port < 1 else str(assigned_route.host_port)
      )
      unit = assigned_route.run_unit_name
      container_port = assigned_route.container_port
      proto = "" if assigned_route.proto == "all" else f"/{assigned_route.proto}"
      lines.append(f":{host_port} → {unit}:{container_port}{proto}")
  else:
    for app_route in stack.routes.values():
      host_port = "<auto>" if app_route.needs_allocation else str(app_route.host_port)
      unit = app_route.run_unit_name
      container_port = app_route.container_port
      proto = "" if app_route.proto == "all" else f"/{app_route.proto}"
      lines.append(f":{host_port} → {unit}:{container_port}{proto}")

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
  """Post-up / status location block (Routes, Host, Data, Logs)."""
  app_id = stack.app
  title = heading if heading is not None else f"Running {app_id}"
  rows: list[tuple[str, str]] = []

  pubs = published_urls(stack, run_data, ctx)
  if pubs:
    rows.append(("Routes:", ", ".join(pubs)))

  hosts = host_port_lines(stack, run_data)
  if hosts:
    rows.append(("Host:", hosts[0]))
    for extra in hosts[1:]:
      rows.append(("", extra))

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
) -> str:
  """Manifest capability summary for inspect / first-run up."""
  app_id = stack.app
  lines: list[str] = [f"{app_id}"]

  images = [f"{name}: {unit.image}" for name, unit in stack.run_units.items()]
  if images:
    lines.append(f"  Images:  {images[0]}")
    for extra in images[1:]:
      lines.append(f"           {extra}")

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
      lines.append(f"  Ports:   {declared_ports[0]}")
      for extra in declared_ports[1:]:
        lines.append(f"           {extra}")

    if stack.subdomain and run_data is not None:
      pubs = published_urls(stack, run_data, ctx)
      if pubs:
        lines.append(f"  Routes:  {pubs[0]}")
        for extra in pubs[1:]:
          lines.append(f"           {extra}")
    elif stack.subdomain:
      lines.append(f"  Routes:  subdomain={stack.subdomain}")

    vols = volume_lines(stack, run_data, ctx)
    if vols:
      lines.append(f"  Volumes: {vols[0]}")
      for extra in vols[1:]:
        lines.append(f"           {extra}")

    required = [
      name
      for name, cfg in stack.config.items()
      if not cfg.has_default() and not cfg.secret
    ]
    secrets = [name for name, cfg in stack.config.items() if cfg.secret]
    if required or secrets:
      parts = []
      if required:
        parts.append(f"required={','.join(required)}")
      if secrets:
        parts.append(f"secrets={','.join(secrets)}")
      lines.append(f"  Config:  {' '.join(parts)}")

  dangers = danger_callouts(stack)
  for danger in dangers:
    lines.append(f"  Danger:  {danger}")

  if compact and not dangers and len(images) == 1:
    return f"  Image:   {images[0].split(': ', 1)[1]}"
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

  pubs = published_urls(stack, run_data, ctx)
  if pubs:
    rows.append(("Routes:", ", ".join(pubs)))

  hosts = host_port_lines(stack, run_data)
  if hosts:
    rows.append(("Host:", hosts[0]))
    for extra in hosts[1:]:
      rows.append(("", extra))

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


def _format_labeled(title: str, rows: list[tuple[str, str]]) -> str:
  width = max((len(label) for label, _ in rows if label), default=0)
  lines = [title]
  for label, value in rows:
    if label:
      lines.append(f"  {label:<{width}}  {value}")
    else:
      lines.append(f"  {'':<{width}}  {value}")
  return "\n".join(lines)


__all__ = [
  "capability_receipt",
  "danger_callouts",
  "host_port_lines",
  "location_receipt",
  "published_urls",
  "status_receipt",
  "volume_lines",
]
