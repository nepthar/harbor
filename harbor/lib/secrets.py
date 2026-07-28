import logging
import re
import secrets
import string

logger = logging.getLogger("harbor.secrets")

_ALNUM = string.ascii_letters + string.digits
_PASSWORD = string.printable
_HEX = string.digits + "abcdef"

_DIRECTIVE_SPLIT = re.compile(r"(\{[^}]*\})")

_MAX_ALNUM = 1024

_DEFAULT_PASS_LENGTH = 16
_DEFAULT_HEX_LENGTH = 8


class SecretGenerationError(ValueError):
  """Pattern cannot be satisfied or is not generate-able."""


def generate_secret(pattern: str) -> str:
  effective = "{alnum:32}" if pattern == "auto" else pattern
  gen_result = _render_pattern(effective)
  if gen_result == pattern:
    raise SecretGenerationError(
      "Pattern did not result in a unique secret - Did you put a literal secret in?"
    )
  return gen_result


def _render_pattern(pattern: str) -> str:
  out: list[str] = []
  for segment in _DIRECTIVE_SPLIT.split(pattern):
    if not segment:
      continue
    if segment.startswith("{") and segment.endswith("}"):
      out.append(_expand_directive(segment[1:-1]))
    else:
      out.append(segment)
  return "".join(out)


def _expand_directive(directive: str) -> str:
  parts = directive.strip().split(":")
  if not parts:
    raise SecretGenerationError("Empty directive")
  keyword = parts[0]
  arg = parts[1] if len(parts) > 1 else None

  match keyword:
    case "alnum":
      generator = _alnum_directive
    case "password":
      generator = _password_directive
    case "hex":
      generator = _hex_directive
    case _:
      raise SecretGenerationError(f"Unknown directive: {keyword}")
  return generator(arg)


def _alnum_directive(arg) -> str:
  length = int(arg) if arg else _DEFAULT_PASS_LENGTH
  return "".join(secrets.choice(_ALNUM) for _ in range(length))


def _password_directive(arg) -> str:
  length = int(arg) if arg else _DEFAULT_PASS_LENGTH
  return "".join(secrets.choice(_PASSWORD) for _ in range(length))


def _hex_directive(arg) -> str:
  length = int(arg) if arg else _DEFAULT_HEX_LENGTH
  return "".join(secrets.choice(_HEX) for _ in range(length * 2))
