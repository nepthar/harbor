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
  PRIMARY_ROUTE_NAME,
  AppConfig,
  AppRoute,
  AppRunUnit,
  AppStack,
  AppVolume,
  BoundVolume,
)
from harbor.lib.util import (
  HAPP_KEY_PREFIX,
  PUBLIC_ROUTE_SCHEME,
  ROUTE_KEY_PREFIX,
  EnvTemplate,
)

logger = getLogger("harbor.run_layout")

# The host clock, bind-mounted into every container. See `_host_mounts`.
LOCALTIME_PATH = "/etc/localtime"


def _project_name(app_id: str) -> str:
  return re.sub(r"[^a-z0-9_-]", "_", app_id.lower())


def _mount_string(bound: BoundVolume) -> str:
  mount = f"{bound.volume.run_rel_path}:{bound.guest_path}"
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

  # True when staging repairs this on its own, so it is not something the
  # operator has to act on. Route allocation is the case: `stage()` clears and
  # reallocates every route *before* it evaluates readiness, so an unallocated
  # route is the normal pre-start state. Counting these as blockers made
  # `harbor ps` report CONFIG as missing for an app that needed no configuration
  # and started fine on the very next `harbor start`.
  self_healing: bool = False

  def line(self) -> str:
    return self.problem


@dataclass(frozen=True)
class VolumeLink:
  """One entry under ``run/<id>/volumes/<kind>/``.

  ``source`` is the real host path, for existence checks and receipts;
  ``target`` is what the symlink itself contains, which is not the same thing
  for `app` volumes -- see :func:`_volume_paths`.
  """

  source: Path
  target: Path
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
  # URL of each route by name; what `${routes.<name>}` in [run.*.env]
  # resolves to. Assigned non-none providers use that provider's domain;
  # otherwise a harbor.localhost placeholder -- see `_route_urls`.
  route_urls: Mapping[str, str]
  # Harbor-managed bind mounts every run unit gets; see `_host_mounts`. Held
  # here rather than read at compose time so that what a host happens to have
  # is decided once, in the one pass that is allowed to look at it.
  host_mounts: tuple[str, ...]
  issues: tuple[ConfigIssue, ...]

  @property
  def stage_blockers(self) -> tuple[ConfigIssue, ...]:
    return tuple(issue for issue in self.issues if issue.stage_blocking)

  @property
  def start_blockers(self) -> tuple[ConfigIssue, ...]:
    """Issues the operator must resolve before the app can start.

    Excludes anything staging repairs itself -- see `ConfigIssue.self_healing`.
    """
    return tuple(issue for issue in self.issues if not issue.self_healing)

  def config_env(self) -> dict[str, str]:
    return {c.env_name(): c.env_val() for c in self.config_values.values()}


def _load_config_values(
  stack: AppStack, issues: list[ConfigIssue], ctx: HarborCtx
) -> dict[str, ConfigValue]:
  store = ctx.app_store(stack.app)
  result = dict()
  for config_name, config in stack.config.items():
    is_secret, value = store.get_config(config_name)
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
      # No value and nothing to fall back on. Harbor cannot invent one, secret
      # or not, so this is the operator's to supply.
      issues.append(
        ConfigIssue(
          f"config {config_name} is unset and no default specified",
          "Set with `harbor config`",
        )
      )
    result[config_name] = ConfigValue(config, resolved)
  return result


def _load_volume_links(
  stack: AppStack, issues: list[ConfigIssue], ctx: HarborCtx
) -> dict[str, VolumeLink]:
  app_id = stack.app
  run_path = ctx.config.run_root / app_id

  found_binds = ctx.app_store(app_id).list_binds()

  volume_links = {}
  for volume_name, volume in stack.volumes.items():
    mkdir = volume.kind not in ("app", "host")
    bind_cmd = f"`harbor config {app_id} --bind {volume_name}=<host_volume>`"

    if volume.kind == "host":
      tag = found_binds.get(volume_name)
      if tag is None:
        issues.append(
          ConfigIssue(
            f"volume {volume_name}: not bound to a host volume",
            f"Bind with {bind_cmd}",
          )
        )
        continue
      host_vol = ctx.config.host_volumes.get(tag)
      if host_vol is None:
        known = ", ".join(sorted(ctx.config.host_volumes)) or "(none)"
        issues.append(
          ConfigIssue(
            f"volume {volume_name}: bound to unknown host volume {tag!r}",
            f"Add [host_volume.{tag}] to config.toml, or re-bind with {bind_cmd}. "
            f"Known host volumes: {known}",
          )
        )
        continue
      if host_vol.readonly and not volume.readonly:
        issues.append(
          ConfigIssue(
            f"volume {volume_name}: host volume {tag!r} is readonly, but the "
            f"app volume is writable",
            f"Declare {volume_name} with readonly = true, or use a writable "
            f"host volume",
          )
        )
        continue
      source = target = host_vol.path
      # Docker would create an empty directory at a missing bind source.
      # Files are allowed — a host volume may be a single file.
      if not source.exists():
        issues.append(
          ConfigIssue(
            f"volume {volume_name}: host volume {tag!r} path does not exist: {source}",
            f"Make {source} available again, or re-bind with {bind_cmd}",
            stage_blocking=True,
          )
        )
        continue
      if host_vol.require_mount and not source.is_mount():
        issues.append(
          ConfigIssue(
            f"volume {volume_name}: host volume {tag!r} is not mounted at {source}",
            f"Mount the share at {source}, or clear require_mount on "
            f"[host_volume.{tag}]",
            stage_blocking=True,
          )
        )
        continue
    else:
      resolved = _volume_paths(run_path, app_id, volume, found_binds, ctx.config)
      if not resolved:
        continue
      source, target = resolved

      # Prevent a bound path that doesn't exist at runtime.
      # Docker would create a folder there otherwise.
      if not source.exists() and not mkdir:
        issues.append(
          ConfigIssue(
            f"volume {volume_name}: host path does not exist: {source}",
            f"re-stage with `harbor stage {app_id}` or this might be a bug.",
          )
        )
        continue

    destination = run_path / volume.run_rel_path
    volume_links[volume_name] = VolumeLink(source, target, destination, mkdir)

  return volume_links


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
          "Clear data with `harbor rm` and retry with `harbor start`",
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
  if stack_route.scheme != conf_route.scheme:
    mismatch("scheme", stack_route.scheme, conf_route.scheme)


def _load_routes(
  stack: AppStack, issues: list[ConfigIssue], ctx: HarborCtx
) -> dict[str, AssignedRoute]:
  found_routes = ctx.harbor_db().list_routes(stack.app)

  missing_routes = set(stack.routes.keys()) - set(found_routes.keys())
  extra_routes = set(found_routes.keys()) - set(stack.routes.keys())

  for name in sorted(missing_routes):
    issues.append(
      ConfigIssue(
        f"route {name}: declared but not allocated",
        "Clear data with `harbor rm` and retry with `harbor start`",
        self_healing=True,
      )
    )
  for name in sorted(extra_routes):
    issues.append(
      ConfigIssue(
        f"route {name}: allocated but not in the manifest",
        "Clear data with `harbor rm` and retry with `harbor start`",
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


def _host_mounts() -> tuple[str, ...]:
  """Binds harbor adds to every run unit, on top of the happ's own [volumes].

  At the moment, it's just the host clock if it exists.

  A happ that wants something else still wins: libc reads /etc/localtime only
  when `TZ` is unset, so `TZ = "Etc/UTC"` in a manifest overrides this.
  """
  if not Path(LOCALTIME_PATH).exists():
    return ()
  return (f"{LOCALTIME_PATH}:{LOCALTIME_PATH}:ro",)


def _route_urls(
  routes: Mapping[str, AssignedRoute],
  assignments: Mapping[str, str],
  config: Config,
) -> dict[str, str]:
  """Where each route answers: provider domain, or harbor.localhost placeholder.

  The URL is always https: TLS terminates at the reverse proxy. `route.scheme`
  is how the proxy dials the backend, not what a browser (or `${routes.x}`)
  should use.
  """
  urls: dict[str, str] = {}
  for name, route in routes.items():
    if not route.subdomain:
      continue
    tag = assignments.get(name)
    domain = config.provider_domain(tag or "")
    urls[name] = f"{PUBLIC_ROUTE_SCHEME}://{route.subdomain}.{domain}"
  return urls


def resolved_subdomain(stack: AppStack, ctx: HarborCtx) -> str | None:
  """The DNS label this install uses: stored config, else the happ default."""
  cfg = stack.config.get("subdomain")
  if cfg is not None:
    _, value = ctx.app_store(stack.app).get_config("subdomain")
    if value:
      return value
    if cfg.has_default():
      return cfg.default
  return stack.subdomain


def _app_domain(
  stack: AppStack,
  assignments: Mapping[str, str],
  config: Config,
  subdomain: str | None,
) -> str | None:
  if subdomain is None:
    return None
  tag = assignments.get(PRIMARY_ROUTE_NAME) or ""
  return f"{subdomain}.{config.provider_domain(tag)}"


def _env_substitutions(
  stack: AppStack, run_unit: AppRunUnit, data: AppRunData
) -> dict[str, str]:
  """Flat key → value map for one unit's `[run.*.env]` placeholders.

  Config keys rewrite to `${__HARBOR_CONFIG__…}` so values (especially secrets)
  stay out of compose.yml and are interpolated from the process env at up.
  `routes.*` and `happ.*` take concrete values.
  """
  volumes = ",".join(
    _env_kvpair(name, bound.guest_path) for name, bound in run_unit.volumes.items()
  )
  cmd = " ".join(run_unit.command) if run_unit.command else ""
  routes = ",".join(
    _env_kvpair(name, str(data.routes[name].container_port)) for name in run_unit.routes
  )
  return {
    **{name: f"${{{cfg.env_name()}}}" for name, cfg in stack.config.items()},
    **{f"{ROUTE_KEY_PREFIX}{name}": url for name, url in data.route_urls.items()},
    f"{HAPP_KEY_PREFIX}domain": data.app_domain or "",
    f"{HAPP_KEY_PREFIX}volumes": volumes,
    f"{HAPP_KEY_PREFIX}cmd": cmd,
    f"{HAPP_KEY_PREFIX}routes": routes,
  }


def make_compose_dict(stack: AppStack, data: AppRunData) -> dict[str, Any]:
  services: dict[str, Any] = {}
  for run_name, run_unit in stack.run_units.items():
    # The manifest validator has already checked that every dotted `${…}`
    # names a known flat key, so the only way one survives unsubstituted is a
    # route that was never allocated -- and `materialize` allocates before it
    # writes.
    environment = {
      str(k): EnvTemplate(str(v)).safe_substitute(
        _env_substitutions(stack, run_unit, data)
      )
      for k, v in run_unit.environment.items()
    }
    labels = {str(k): str(v) for k, v in run_unit.labels.items()}
    if data.app_domain:
      labels[HARBOR_SUBDOMAIN_LABEL] = data.app_domain

    service: dict[str, Any] = {
      "image": run_unit.image,
      "hostname": run_unit.hostname,
    }

    service["restart"] = run_unit.restart or "unless-stopped"

    mounts = [_mount_string(bound) for bound in run_unit.volumes.values()]
    # Harbor's own mounts come last, and stay out of `${happ.volumes}`: that
    # value tells a happ where the volumes it declared ended up, and it
    # declared none of these.
    mounts.extend(data.host_mounts)
    if mounts:
      service["volumes"] = mounts

    if run_unit.command:
      service["command"] = list(run_unit.command)

    if run_unit.routes:
      service["ports"] = [
        _port_string(
          data.routes[name].host_port,
          data.routes[name].container_port,
          data.routes[name].proto,
        )
        for name in run_unit.routes
      ]

    if stack.network_mode == "host":
      service["network_mode"] = "host"

    if labels:
      service["labels"] = labels

    service["environment"] = environment

    # Manifest [run.<unit>.compose] passthrough; the manifest validator
    # guarantees it never shadows a harbor-managed key.
    service.update(run_unit.compose_extra)

    services[str(run_name)] = service

  return {
    "name": _project_name(str(stack.app)),
    "services": services,
  }


def load_run_data(stack: AppStack, ctx: HarborCtx) -> AppRunData:
  issues: list[ConfigIssue] = []
  run_path = ctx.staged_paths(stack.app).run_path
  config_values = _load_config_values(stack, issues, ctx)
  routes = _load_routes(stack, issues, ctx)
  vol_links = _load_volume_links(stack, issues, ctx)
  assignments = ctx.app_store(stack.app).list_route_assignments()
  return AppRunData(
    app=stack.app,
    run_path=run_path,
    app_domain=_app_domain(
      stack, assignments, ctx.config, resolved_subdomain(stack, ctx)
    ),
    volume_links=vol_links,
    config_values=config_values,
    routes=routes,
    route_urls=_route_urls(routes, assignments, ctx.config),
    host_mounts=_host_mounts(),
    issues=tuple(issues),
  )


def _volume_paths(
  run_path: Path,
  app_id: str,
  volume: AppVolume,
  binds: dict[str, str],
  config: Config,
) -> tuple[Path, Path] | None:
  """The (host path, symlink target) for a volume, or None if unresolvable.

  `app` links are relative because they point inside the run dir, so they stay
  correct wherever that directory is restored. Managed and `host` links are
  absolute: `volume_roots` / host_volume paths are configurable precisely so
  they can live on another disk, which makes those links meaningful only on
  the machine that made them.
  """
  match volume.kind:
    case "app":
      src = volume.src if volume.src else volume.name
      return run_path / "happ" / src, Path("../../happ") / src
    case "host":
      tag = binds.get(volume.name)
      if tag is None:
        return None
      host_vol = config.host_volumes.get(tag)
      if host_vol is None:
        return None
      return host_vol.path, host_vol.path
    case other:
      path = config.volume_roots[other] / app_id / volume.name
      return path, path
