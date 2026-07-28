from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Any, Literal

from harbor.lib.apps import AppID
from harbor.lib.config import Config
from harbor.lib.harbor import HarborCtx
from harbor.lib.stack import (
  HARBOR_SUBDOMAIN_LABEL,
  AppConfig,
  AppRoute,
  AppStack,
  AppVolume,
  BoundVolume,
)

logger = getLogger("harbor.run_layout")


def _project_name(app_id: str) -> str:
  return re.sub(r"[^a-z0-9_-]", "_", app_id.lower())


def _mount_string(volume_name: str, bound: BoundVolume) -> str:
  mount = f"./volumes/{volume_name}:{bound.guest_path}"
  if bound.readonly:
    mount += ":ro"
  return mount


def _env_kvpair(key: str, val: str) -> str:
  return f"{key}:{val}"


def _port_string(host_port: int, container_port: int, proto: str) -> str:
  if proto == "udp":
    return f"{host_port}:{container_port}/udp"
  if proto in ("tcp", "all"):
    return f"{host_port}:{container_port}"
  return f"{host_port}:{container_port}/{proto}"


@dataclass(frozen=True)
class ConfigIssue:
  """A per-installation requirement that is still unmet."""

  problem: str
  fix: str | None

  stage_blocking: bool = False

  # True when `up` repairs this on its own, so it is not something the operator
  # has to act on. Route allocation is the case: `stage()` clears and
  # reallocates every route *before* it evaluates readiness, so an unallocated
  # route is the normal pre-`up` state. Counting these as blockers made
  # `harbor ps` report "needs config" for an app that needed no configuration
  # and started fine on the very next `up`.
  self_healing: bool = False

  def line(self) -> str:
    return self.problem


@dataclass(frozen=True)
class VolumeLink:
  source: Path
  destination: Path
  mkdir: bool


@dataclass(frozen=True)
class ConfigValue:
  config: AppConfig
  value: str | None

  def env_name(self) -> str:
    return self.config.env_name()

  def env_val(self) -> str:
    return self.value if self.value is not None else ""


@dataclass(frozen=True)
class AssignedRoute:
  name: str
  subdomain: str
  run_unit_name: str
  host_port: int
  container_port: int
  proto: str
  publish: Literal["web", "lan"]
  scheme: Literal["http", "https"]


@dataclass(frozen=True)
class AppRunData:
  """The "Runtime app data" that is used to materialize and run an AppStack
  In any other language, this would be immutable, but here we are.
  """

  app: AppID
  run_path: Path
  app_domain: str | None
  volume_links: Mapping[str, VolumeLink]
  config_values: Mapping[str, ConfigValue]
  routes: Mapping[str, AssignedRoute]
  issues: tuple[ConfigIssue, ...]

  @property
  def stage_blockers(self) -> tuple[ConfigIssue, ...]:
    return tuple(issue for issue in self.issues if issue.stage_blocking)

  @property
  def start_blockers(self) -> tuple[ConfigIssue, ...]:
    """Issues the operator must resolve before the app can start.

    Excludes anything `up` repairs itself -- see `ConfigIssue.self_healing`.
    """
    return tuple(issue for issue in self.issues if not issue.self_healing)

  def config_env(self) -> dict[str, str]:
    return {c.env_name(): c.env_val() for c in self.config_values.values()}


def _load_config_values(
  stack: AppStack, issues: list[ConfigIssue], ctx: HarborCtx
) -> dict[str, ConfigValue]:
  app_db = ctx.app_db(stack.app)
  result = dict()
  for config_name, config in stack.config.items():
    is_secret, value = app_db.get_config(config_name)
    resolved = value
    if value is not None:
      if is_secret != config.secret:
        resolved = None
        issues.append(
          ConfigIssue(
            f"config {config_name} expected secret={config.secret}, but found secret={is_secret}",
            "Overwrite existing value with `harbor config`",
          )
        )
    elif config.has_default():
      resolved = config.default
    else:
      # No value and nothing to fall back on. `up` cannot invent one, secret or
      # not, so this is the operator's to supply.
      issues.append(
        ConfigIssue(
          f"config {config_name} is unset and no default specified",
          "Set with `harbor config`",
        )
      )
    result[config_name] = ConfigValue(config, resolved)
  return result


def _load_volume_links(
  stack: AppStack,
  issues: list[ConfigIssue],
  ctx: HarborCtx,
  app_path: Path | None = None,
) -> dict[str, VolumeLink]:
  app_db = ctx.app_db(stack.app)

  app_id = stack.app
  app_path = app_path or ctx.app_path(stack.app)
  run_path = ctx.config.run_root / app_id

  found_binds = {
    name: Path(entry["host_path"]) for name, entry in app_db.list_binds().items()
  }

  volume_links = {}
  for volume_name, volume in stack.volumes.items():
    source = _resolve_volume_host_path(
      app_path, app_id, volume, found_binds, ctx.config
    )

    mkdir = volume.kind not in ("app", "ext")

    if not source:
      issues.append(
        ConfigIssue(
          f"volume {volume_name}: not bound to a host path",
          "Bind with `harbor config <app_id> --bind`",
        )
      )
    elif not source.exists() and not mkdir:
      issues.append(
        ConfigIssue(
          f"volume {volume_name}: host path does not exist: {source}",
          "Bind with `harbor config <app_id> --bind`",
        )
      )
    else:
      destination = run_path / volume.run_rel_path
      volume_links[volume_name] = VolumeLink(source, destination, mkdir)

  return volume_links if volume_links else {}


def _compare_route(
  issues: list[ConfigIssue], stack_route: AppRoute, conf_route: AssignedRoute
) -> None:
  # Note: In the current impl, we ALWAYS clear out all configured routes before staging/running
  # so any stale data should be gone. I'm leaving this here out of caution for future work.
  def mismatch(field: str, from_stack: Any, from_config: Any):
    issues.append(
      ConfigIssue(
        f"route {stack_route.route_name}: {field} mismatch: stack={from_stack} config={from_config}",
        "Examine w/ `harbor routes`, remove app data & runtime with `harbor rm`",
        self_healing=True,
      )
    )

  if stack_route.route_name != conf_route.name:
    mismatch("name", stack_route.route_name, conf_route.name)
  if stack_route.run_unit_name != conf_route.run_unit_name:
    mismatch("run unit", stack_route.run_unit_name, conf_route.run_unit_name)

  if stack_route.needs_allocation:
    if conf_route.host_port == -1:
      issues.append(
        ConfigIssue(
          f"route {stack_route.route_name}: host port not allocated",
          "Clear data with `harbor rm` and retry with `harbor up",
          self_healing=True,
        )
      )
  else:
    if stack_route.host_port != conf_route.host_port:
      mismatch("host port", stack_route.host_port, conf_route.host_port)
  if stack_route.container_port != conf_route.container_port:
    mismatch("container port", stack_route.container_port, conf_route.container_port)
  if stack_route.proto != conf_route.proto:
    mismatch("proto", stack_route.proto, conf_route.proto)
  if stack_route.publish != conf_route.publish:
    mismatch("publish mode", stack_route.publish, conf_route.publish)
  if stack_route.scheme != conf_route.scheme:
    mismatch("scheme", stack_route.scheme, conf_route.scheme)


def _load_routes(
  stack: AppStack, issues: list[ConfigIssue], ctx: HarborCtx
) -> dict[str, AssignedRoute]:
  app_db = ctx.app_db(stack.app)

  found_routes = app_db.list_routes()

  missing_routes = set(stack.routes.keys()) - set(found_routes.keys())
  extra_routes = set(found_routes.keys()) - set(stack.routes.keys())

  for name in sorted(missing_routes):
    issues.append(
      ConfigIssue(
        f"route {name}: declared but not allocated",
        "Clear data with `harbor rm` and retry with `harbor up`",
        self_healing=True,
      )
    )
  for name in sorted(extra_routes):
    issues.append(
      ConfigIssue(
        f"route {name}: allocated but not in the manifest",
        "Clear data with `harbor rm` and retry with `harbor up`",
        self_healing=True,
      )
    )

  loaded = {}
  for name, route_entry in found_routes.items():
    configd = AssignedRoute(**route_entry)
    from_stack = stack.routes.get(name)
    if from_stack is None:
      # We already added an issue for extra/missing routes.
      continue

    _compare_route(issues, from_stack, configd)
    loaded[name] = configd

  return loaded


def make_compose_dict(stack: AppStack, data: AppRunData) -> dict[str, Any]:
  services: dict[str, Any] = {}
  for run_name, run_unit in stack.run_units.items():
    environment = {str(k): str(v) for k, v in run_unit.environment.items()}
    labels = {str(k): str(v) for k, v in run_unit.labels.items()}
    if data.app_domain:
      environment["HAPP_DOMAIN"] = data.app_domain
      labels[HARBOR_SUBDOMAIN_LABEL] = data.app_domain

    service: dict[str, Any] = {
      "image": run_unit.image,
      "hostname": run_unit.hostname,
    }

    service["restart"] = run_unit.restart or "unless-stopped"

    if run_unit.volumes:
      service["volumes"] = [
        _mount_string(volname, bound) for volname, bound in run_unit.volumes.items()
      ]
      volstr = ",".join(
        _env_kvpair(volname, bound.guest_path)
        for volname, bound in run_unit.volumes.items()
      )
      environment["HAPP_VOLUMES"] = volstr

    if run_unit.command:
      environment["HAPP_CMD"] = " ".join(run_unit.command)
      service["command"] = list(run_unit.command)

    if run_unit.routes:
      svc_ports = []
      routes_env = []
      for port_name, _ in run_unit.routes.items():
        route = data.routes[port_name]
        svc_ports.append(
          _port_string(route.host_port, route.container_port, route.proto)
        )
        routes_env.append(_env_kvpair(port_name, str(route.container_port)))

      service["ports"] = svc_ports
      environment["HAPP_ROUTES"] = ",".join(routes_env)

    if stack.network_mode == "host":
      service["network_mode"] = "host"

    if labels:
      service["labels"] = labels

    service["environment"] = environment

    services[str(run_name)] = service

  return {
    "name": _project_name(str(stack.app)),
    "services": services,
  }


def load_run_data(
  stack: AppStack, ctx: HarborCtx, app_path: Path | None = None
) -> AppRunData:
  issues: list[ConfigIssue] = []
  run_path = ctx.run_path(stack.app)
  config_values = _load_config_values(stack, issues, ctx)
  routes = _load_routes(stack, issues, ctx)
  vol_links = _load_volume_links(stack, issues, ctx, app_path)
  app_domain = (
    f"{stack.subdomain}.{ctx.config.domain}" if stack.subdomain is not None else None
  )
  return AppRunData(
    app=stack.app,
    run_path=run_path,
    app_domain=app_domain,
    volume_links=vol_links,
    config_values=config_values,
    routes=routes,
    issues=tuple(issues),
  )


def _resolve_volume_host_path(
  app_path: Path, app_id: str, volume: AppVolume, binds: dict[str, Path], config: Config
) -> Path | None:
  match volume.kind:
    case "app":
      src = volume.src if volume.src else volume.name
      return app_path / src
    case "ext":
      return binds.get(volume.name)
    case other:
      return config.volume_roots[other] / app_id / volume.name
