from __future__ import annotations

import base64
import hashlib
from typing import Protocol

from cryptography.fernet import Fernet

from harbor.lib.config import Config


class CryptoEngine(Protocol):
  def encrypt(self, plaintext: str) -> str: ...

  def decrypt(self, ciphertext: str) -> str: ...


def crypto_from_config(config: Config) -> CryptoEngine:
  if config.master_key:
    return FernetCryptoEngine(config.master_key)
  return NoopCryptoEngine()


class NoopCryptoEngine:
  def encrypt(self, plaintext: str) -> str:
    return plaintext

  def decrypt(self, ciphertext: str) -> str:
    return ciphertext


class FernetCryptoEngine:
  def __init__(self, master_key: str):
    digest = hashlib.sha256(master_key.encode()).digest()
    self._fernet = Fernet(base64.urlsafe_b64encode(digest))

  def encrypt(self, plaintext: str) -> str:
    return self._fernet.encrypt(plaintext.encode()).decode()

  def decrypt(self, ciphertext: str) -> str:
    return self._fernet.decrypt(ciphertext.encode()).decode()
