"""The self-signed certificate the https listener serves.

This app carries a password to an API that can run every harbor verb, so it
terminates TLS itself rather than trusting whatever sits in front of it. A
homelab has no CA, so the certificate is self-signed: a browser warns once,
and the fingerprint stays put because the key lives in a `data` volume.
"""

import datetime
import ipaddress
import os
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALID_DAYS = 3650


def names():
  """Every name this server may be reached by, the route's domain first."""
  found = [os.environ.get("TLS_DOMAIN", "").strip(), socket.gethostname(), "localhost"]
  return list(dict.fromkeys(name for name in found if name))


def ensure(directory):
  """Write cert.pem and key.pem into `directory` unless they already fit."""
  folder = Path(directory)
  folder.mkdir(parents=True, exist_ok=True)
  cert, key = folder / "cert.pem", folder / "key.pem"
  wanted = names()
  if cert.exists() and key.exists() and _covers(cert, wanted):
    return cert, key
  _generate(cert, key, wanted)
  return cert, key


def _covers(cert, wanted):
  """Whether an existing certificate still carries every name we want."""
  try:
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    san = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName)
  except Exception:
    return False
  return set(wanted) <= set(san.value.get_values_for_type(x509.DNSName))


def _generate(cert, key, wanted):
  private = ec.generate_private_key(ec.SECP256R1())
  subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, wanted[0])])
  now = datetime.datetime.now(datetime.UTC)
  alt = [x509.DNSName(name) for name in wanted]
  alt += [x509.IPAddress(ipaddress.ip_address(a)) for a in ("127.0.0.1", "::1")]
  builder = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(subject)
    .public_key(private.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - datetime.timedelta(minutes=5))
    .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
    .add_extension(x509.SubjectAlternativeName(alt), critical=False)
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
  )
  signed = builder.sign(private, hashes.SHA256())

  key.write_bytes(
    private.private_bytes(
      serialization.Encoding.PEM,
      serialization.PrivateFormat.PKCS8,
      serialization.NoEncryption(),
    )
  )
  key.chmod(0o600)
  cert.write_bytes(signed.public_bytes(serialization.Encoding.PEM))


if __name__ == "__main__":
  ensure(sys.argv[1])
