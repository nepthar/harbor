import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("harbor.logtab")


class LogTab:
  """LogTab is a logging table which stores key-value pairs.
  This is useful for when you wish to have a key-value store which
  along with the entire history of changes in an auditable log.

  The format is:
  <date>\t<operation>\t<key>\t<value>\n
  # comments begin with a hash.

  An operation is one of:
  - set: set a value
  - del: delete a value
  - clr: clear all values matching a prefix

  "Invalid" lines are skipped with a warning unless the LogTab is
  initialized with strict=True.
  """

  FS = "\t"
  KEY_RE = re.compile(r"[a-zA-Z0-9_/.-]+\Z")
  KEY_LEN = 512
  VAL_LEN = 1024

  @staticmethod
  def ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

  @staticmethod
  def validate_key(key: str):
    if not key or len(key) > LogTab.KEY_LEN or LogTab.KEY_RE.fullmatch(key) is None:
      raise ValueError(f"Invalid logtab key: {key}")

  @staticmethod
  def validate_value(val: str):
    if len(val) > LogTab.VAL_LEN or "\n" in val:
      raise ValueError(f"Invalid logtab value: {val}")

  def __init__(self, path: Path, title: str = "", strict: bool = False):
    self.strict = strict
    self.path = path
    if not path.is_file():
      header = []
      if title:
        header.append(f"# {title}")
      header.append(f"# {path.name}, created {LogTab.ts()}")
      header.append("# Format: <date>\\t<set|del|clr>\\t<key>\\t<value>\\n")
      header = "\n".join(header)
      self._append_entry(header)

  def _append_entry(self, chunk):
    chunk = chunk + "\n"
    with open(self.path, "a") as f:
      f.write(chunk)

  def _value_err(self, errmsg: str) -> None:
    if self.strict:
      raise ValueError(errmsg)
    else:
      logger.error(errmsg)

  def load(self) -> dict[str, str]:
    """Materialize the table into a dictionary"""
    results = {}
    with open(self.path) as f:
      for line_number, line in enumerate(f, start=1):
        if line.startswith("#") or not line.strip():
          continue
        split = line.strip("\n").split(LogTab.FS, 3)
        if len(split) != 4:
          errmsg = f"Skipping malformed logtab record at {self.path}:{line_number}: {line.rstrip('\n')}"
          self._value_err(errmsg)
          continue

        _ts, operation, key, value = split
        match operation:
          case "set":
            results[key] = value
          case "del":
            results.pop(key, None)
          case "clr":
            for k in [k for k in results if k.startswith(key)]:
              results.pop(k)
          case _:
            errmsg = f"Skipping malformed logtab record at {self.path}:{line_number}: {line.rstrip('\n')}"
            self._value_err(errmsg)
            continue

    return results

  def write(self, key: str, value: str):
    """Write a single value to the table"""
    LogTab.validate_key(key)
    LogTab.validate_value(value)

    line = LogTab.FS.join((LogTab.ts(), "set", key, value))
    self._append_entry(line)

  def read(self, key: str) -> str | None:
    """Read a single value from the table by exact match"""
    return self.load().get(key)

  def clear(self, prefix: str, comment: str = "") -> None:
    """Clear all values matching the given prefix.
    The optional comment is stored on the line, but never used by the table.
    """
    LogTab.validate_key(prefix)
    self._append_entry(LogTab.FS.join((LogTab.ts(), "clr", prefix, comment)))

  def delete(self, key: str, comment: str = "") -> None:
    """Delete a single key by exact match.
    The optional comment is stored on the line, but never used by the table.
    """
    LogTab.validate_key(key)
    if self.strict and key not in self.load():
      raise ValueError(f"Key {key} not found in logtab {self.path} for deletion")
    self._append_entry(LogTab.FS.join((LogTab.ts(), "del", key, comment)))

  def scan(self, prefix: str = "", suffix: str = "") -> dict[str, str]:
    """Scan the table for values matching the given prefix and suffix
    If no prefix or suffix is given, this is the same as load()
    """
    if prefix:
      LogTab.validate_key(prefix)
    if suffix:
      LogTab.validate_key(suffix)

    data = self.load()
    return {
      k: v for k, v in data.items() if k.startswith(prefix) and k.endswith(suffix)
    }
