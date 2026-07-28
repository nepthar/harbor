import argparse
import secrets

from harbor.cli.kv import parse_kv
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


def run_gen_masterkey(args: argparse.Namespace, ctx: HarborCtx, conn) -> None:
  mkey_file = ctx.config.master_keyfile
  LogTab(mkey_file).write("master_key", secrets.token_hex(128))
  conn.out(f"New master key appended to: {mkey_file}")
