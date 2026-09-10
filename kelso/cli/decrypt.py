import argparse

from cryptography.fernet import InvalidToken

from kelso.lib.crypto import FernetCryptoEngine
from kelso.lib.kelso import KelsoCtx


def register(subparsers) -> None:
  parser = subparsers.add_parser(
    "decrypt",
    help="Decrypt a value from stdin using kelso's master key",
  )
  # Reads nothing from kelsodb and writes nothing anywhere.
  parser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: KelsoCtx, conn) -> None:
  # Not crypto_from_config: with no master key that hands back the noop engine,
  # which returns its input unchanged and would report every blob as decrypted.
  if not ctx.config.master_key:
    raise ValueError(
      f"No master key in {ctx.config.master_keyfile}, so nothing was encrypted "
      f"with one. Run: kelso config-sys gen-masterkey"
    )

  blob = conn.read().strip()
  if not blob:
    raise ValueError("Nothing on stdin to decrypt")

  try:
    # A Fernet token carries an HMAC over its own contents, so this either
    # returns the original plaintext or raises -- there is no wrong-key result
    # that decrypts to plausible garbage.
    plaintext = FernetCryptoEngine(ctx.config.master_key).decrypt(blob)
  except InvalidToken:
    raise ValueError(
      "Could not decrypt that value. It is not a kelso-encrypted blob, or it "
      "was encrypted with a different master key"
    ) from None

  conn.out(plaintext)
