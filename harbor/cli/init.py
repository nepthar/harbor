import argparse
import os
import secrets
from pathlib import Path

from harbor.lib.config import VOLUME_KINDS, load_config_file
from harbor.lib.logtab import LogTab

DEFAULT_ROOT = Path("~/.harbor")

CONFIG_TEMPLATE = """\
# Harbor configuration — edit this file to change your setup.
# Paths are relative to the directory containing this file unless absolute.

apps_root = "apps"
run_root = "run"
volume_root = "volumes"
master_keyfile = "master.key"
port_base = 41000

# Optional: extra directories to look for happs in, on top of apps_root above
# (which is always the "apps" source). Repeat the block for more. An app id
# carried by two sources is ambiguous; `harbor doctor` reports those, and you
# stage one by passing its full path.
#
# [[app_source]]
# name     = "dev"
# location = "~/code/happs"

# The address by which harbor is reachable on your network, used for setting
# up routes. Every route provider that proxies traffic points at it, so it is
# required as soon as one is configured.
# harbor_address = "10.0.0.5"

# Routes are auto-assigned to this provider tag on first stage (like a config
# default), unless marked private=true in the manifest. The reserved tag
# "none" is a built-in noop and is the default when this key is omitted.
# default_route_provider = "web"

# Optional: reverse-proxy (or other) providers that publish app routes.
# Each block is tagged by you ("web", "lan", "homelab", …); `kind` selects
# the implementation. Kind-specific settings go under `args`. Store the
# password with `harbor config-sys --stdin route_provider.web.password`,
# then verify with `harbor routes check web`. Assign routes with
# `harbor config <app> --route main=web`.
#
# [route_provider.web]
# kind   = "nginx_proxy_manager"
# domain = "example.com"
# [route_provider.web.args]
# endpoint        = "http://npm-host:81"
# email           = "admin@example.com"
# password_secret = "route_provider.web.password"

# Pangolin publishes each route as a public HTTP resource on `domain`, with a
# target on `site` pointing at harbor_address. `endpoint` must be https -- the
# API key is a bearer token on every call. `org_id` and `site` are the names in
# the Pangolin dashboard URL: .../<org_id>/settings/sites/<site>/general. Store
# the key with `harbor config-sys --stdin route_provider.tunnel.api_key`.
#
# [route_provider.tunnel]
# kind   = "pangolin"
# domain = "example.com"
# [route_provider.tunnel.args]
# endpoint       = "https://pangolin-host:3003"
# org_id         = "my-org"
# site           = "substantial-atractaspis-branchi"
# api_key_secret = "route_provider.tunnel.api_key"

# Optional: tagged host paths that apps with kind = "host" volumes can bind to.
# Paths must exist before `harbor config|start --bind`. Assign with
# `harbor config <app> --bind media=media`.
# Set require_mount = true for network shares or external drives so an empty
# mount-point directory is refused when the share is not mounted.
#
# [host_volume.media]
# path          = "/mnt/media"
# readonly      = true
# require_mount = true
"""


def register(subparsers) -> None:
  parser = subparsers.add_parser("init", help="Initialize a harbor root directory")
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, _ctx, conn) -> None:
  default = Path(os.environ.get("HARBOR_ROOT", DEFAULT_ROOT)).expanduser()
  if getattr(args, "root", None):
    default = Path(args.root).expanduser()
  response = conn.read(f"Harbor root directory [{default}]: ").strip()
  root = Path(response if response else default).expanduser().resolve()

  if root.exists() and not root.is_dir():
    conn.err(f"Error: {root} exists and is not a directory")
    raise SystemExit(1)

  config_path = root / "config.toml"
  if config_path.exists():
    conn.err(f"Error: config already exists at {config_path}")
    conn.err("If you want to re-initialize, remove it first.")
    raise SystemExit(1)

  (root / "apps").mkdir(parents=True, exist_ok=True)
  (root / "run").mkdir(parents=True, exist_ok=True)
  (root / "config").mkdir(parents=True, exist_ok=True)
  for kind in VOLUME_KINDS:
    (root / "volumes" / kind).mkdir(parents=True, exist_ok=True)

  config_path.write_text(CONFIG_TEMPLATE)

  master_key_path = root / "master.key"
  LogTab(master_key_path, title="Harbor Master Key").write(
    "master_key", secrets.token_hex(128)
  )
  master_key_path.chmod(0o600)

  load_config_file(config_path)

  conn.out(f"Initialized harbor root at {root}")
  conn.out(f"  apps:        {root / 'apps'}")
  conn.out(f"  run:         {root / 'run'}")
  conn.out(f"  config:      {root / 'config'}")
  conn.out(f"  volumes:     {root / 'volumes'} ({', '.join(VOLUME_KINDS)})")
  conn.out(f"  master.key:  {master_key_path}")
  conn.out(f"\nTo change your configuration, edit {config_path}")
  conn.out(
    "\nNext: put a happ in apps/ (or `harbor fetch <target>`), then\n"
    "  harbor stage <app>   install it into run/ without starting it\n"
    "  harbor start <app>   start it (staging first if needed)\n"
    "  harbor stop <app>    stop it"
  )

  default_root = DEFAULT_ROOT.expanduser().resolve()
  if root != default_root:
    conn.out(
      f"\nThis root is not the default. Persist it with:\n"
      f"  export HARBOR_ROOT={root}\n"
      f"or pass `--root {root}` on every harbor command."
    )
