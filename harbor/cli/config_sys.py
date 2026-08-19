import argparse
import secrets

from tabulate import tabulate

from harbor.cli.kv import parse_kv
from harbor.lib.config_edit import add_host_volume, remove_host_volume, set_host_volume
from harbor.lib.harbor import HarborCtx
from harbor.lib.logtab import LogTab


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "config-sys",
    help="List or set encrypted system config",
  )
  parser.add_argument(
    "--set",
    action="append",
    default=[],
    dest="sets",
    metavar="KEY=VALUE",
    help="Set a system config value (repeatable)",
  )
  parser.add_argument(
    "--unset",
    action="append",
    default=[],
    dest="unsets",
    metavar="KEY",
    help="Remove a system config (repeatable)",
  )
  parser.add_argument(
    "--stdin",
    dest="stdin_key",
    metavar="KEY",
    help="Set KEY from stdin (for secrets)",
  )
  parser.set_defaults(func=run)

  sub = parser.add_subparsers(dest="config_sys_command", required=False)
  gen = sub.add_parser("gen-masterkey", help="Generate a new master key")
  gen.set_defaults(func=run_gen_masterkey)

  hv = sub.add_parser(
    "host-volume",
    help="List, add, change or remove [host_volume] entries in config.toml",
  )
  hv.add_argument("--add", metavar="TAG=PATH", help="Declare a new host volume")
  hv.add_argument(
    "--set", dest="set_", metavar="TAG=PATH", help="Replace an existing one"
  )
  hv.add_argument("--rm", metavar="TAG", help="Remove one")
  hv.add_argument(
    "--readonly",
    action="store_true",
    help="With --add/--set: apps may only mount it read-only",
  )
  hv.add_argument(
    "--require-mount",
    action="store_true",
    help="With --add/--set: refuse to start unless the path is a mount point",
  )
  hv.set_defaults(func=run_host_volume)


def run(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  db = ctx.harbor_db()
  changed = False

  if args.stdin_key is not None:
    value = conn.read().rstrip("\n")
    if not value:
      raise ValueError("empty value")
    db.set_secret(args.stdin_key, value)
    conn.out(f"Set system config {args.stdin_key!r}")
    changed = True

  for raw in args.sets:
    name, value = parse_kv(raw, "--set")
    db.set_secret(name, value)
    conn.out(f"Set system config {name!r}")
    changed = True

  for name in args.unsets:
    db.del_secret(name)
    conn.out(f"Unset system config {name!r}")
    changed = True

  if changed:
    return

  for name in db.list_secrets():
    conn.out(name)


def run_host_volume(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  chosen = [flag for flag in (args.add, args.set_, args.rm) if flag]
  if len(chosen) > 1:
    raise ValueError("Use one of --add, --set or --rm at a time")

  if args.add:
    tag, path = parse_kv(args.add, "--add")
    add_host_volume(
      ctx,
      tag,
      path,
      readonly=args.readonly,
      require_mount=args.require_mount,
    )
    conn.out(f"Added host volume {tag} -> {path}")
    return

  if args.set_:
    tag, path = parse_kv(args.set_, "--set")
    set_host_volume(
      ctx,
      tag,
      path,
      readonly=args.readonly,
      require_mount=args.require_mount,
    )
    conn.out(f"Set host volume {tag} -> {path}")
    return

  if args.rm:
    remove_host_volume(ctx, args.rm)
    conn.out(f"Removed host volume {args.rm}")
    return

  rows = [
    (
      tag,
      str(volume.path),
      "yes" if volume.readonly else "",
      "yes" if volume.require_mount else "",
    )
    for tag, volume in sorted(ctx.config.host_volumes.items())
  ]
  conn.out(
    tabulate(rows, headers=["tag", "path", "readonly", "require_mount"])
    if rows
    else "No host volumes declared."
  )


def run_gen_masterkey(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  mkey_file = ctx.config.master_keyfile
  LogTab.write_entry(mkey_file, "master_key", "set", secrets.token_hex(128))
  conn.out(f"New master key appended to: {mkey_file}")
