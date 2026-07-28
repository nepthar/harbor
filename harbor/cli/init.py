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
domain = "harbor.localhost"
port_base = 41000

# Optional: attach HTTP routes for happs with web routes (publish = "web")
# through an external reverse proxy. Without this section, routing is skipped
# and ports are still published to the LAN. Store the password with
# `harbor config-sys --stdin route_provider.nginx_proxy_manager.password`, then
# verify with `harbor routes check`.
#
# [route_provider.nginx_proxy_manager]
# endpoint        = "http://npm-host:81"
# email           = "admin@example.com"
# password_secret = "route_provider.nginx_proxy_manager.password"
# forward_host    = "10.0.0.5"
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
  for kind in VOLUME_KINDS:
    (root / "volumes" / kind).mkdir(parents=True, exist_ok=True)

  config_path.write_text(CONFIG_TEMPLATE)

  master_key_path = root / "master.key"
  LogTab(master_key_path, title="Harbor Master Key").write(
    "master_key", secrets.token_hex(128)
  )
  master_key_path.chmod(0o600)

  load_config_file(config_path, "cli")

  conn.out(f"Initialized harbor root at {root}")
  conn.out(f"  apps:        {root / 'apps'}")
  conn.out(f"  run:         {root / 'run'}")
  conn.out(f"  volumes:     {root / 'volumes'} ({', '.join(VOLUME_KINDS)})")
  conn.out(f"  master.key:  {master_key_path}")
  conn.out(f"\nTo change your configuration, edit {config_path}")

  default_root = DEFAULT_ROOT.expanduser().resolve()
  if root != default_root:
    conn.out(
      f"\nThis root is not the default. Persist it with:\n"
      f"  export HARBOR_ROOT={root}\n"
      f"or pass `--root {root}` on every harbor command."
    )
