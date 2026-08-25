import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("harbor.logtab")


class LogTab:
  """LogTab is a logging table which stores key-value pairs.
  This is useful for when you wish to have a key-value store which
  along with the entire history of changes in an auditable log.

  Logtab is NOT a performant database or a clever way to optimize for writes.
  It is a simple key-value store with a history of changes.

  The format is:
  <date>\t<operation>\t<key>\t<value>\n
  # comments begin with a hash.

  An operation is one of:
  - set: set a value
  - del: delete a value
  - clr: clear all values matching a prefix

  "Invalid" lines are skipped with a warning unless the LogTab is
  initialized with strict=True.

  - Records are appended with one O_APPEND write so concurrent writers cannot
    interleave complete writes on a local POSIX filesystem.
  - Each `read` loops through the whole file. If you're looking up lots of keys, consider calling `load` instead.
  """

  @dataclass(frozen=True, slots=True)
  class Entry:
    ts: str
    value: str

    def datetime(self) -> datetime:
      return datetime.fromisoformat(self.ts)

  FS = "\t"
  KEY_RE = re.compile(r"[a-zA-Z0-9_/.-]+\Z")
  KEY_LEN = 512
  VAL_LEN = 1024

  @staticmethod
  def ts() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

  @staticmethod
  def validate_key(key: str):
    if not key or len(key) > LogTab.KEY_LEN or LogTab.KEY_RE.fullmatch(key) is None:
      raise ValueError(f"Invalid logtab key: {key}")

  @staticmethod
  def validate_value(val: str):
    if len(val) > LogTab.VAL_LEN or "\n" in val:
      raise ValueError(f"Invalid logtab value or comment: {val}")

  @staticmethod
  def validate_operation(operation: str):
    if operation not in ("set", "del", "clr"):
      raise ValueError(f"Invalid logtab operation: {operation}")

  @staticmethod
  def write_entry(path: Path, key: str, operation: str, value: str = "") -> None:
    LogTab.validate_key(key)
    LogTab.validate_operation(operation)
    LogTab.validate_value(value)
    line = LogTab.FS.join((LogTab.ts(), operation, key, value))

    data = (line + "\n").encode()
    data_len = len(data)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    one_call = True
    try:
      offset = 0
      while offset < data_len:
        written = os.write(fd, data[offset:])
        if written < data_len - offset:
          one_call = False
        offset += written
    finally:
      os.close(fd)

    if not one_call:
      # This unlikely, but possible if the filesystem is under heavy load,
      # or is full, or over a network or something like that. It is definitly not typical.
      logger.error(
        "Adding entry %s to %s did not happen atomically - corruption possible",
        line,
        path,
      )

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
      with open(path, "w") as f:
        f.write(header + "\n")

  def _value_err(self, errmsg: str) -> None:
    if self.strict:
      raise ValueError(errmsg)
    else:
      logger.error(errmsg)

  def load(self) -> dict[str, Entry]:
    """Materialize the table into a dictionary of Entries."""
    results: dict[str, LogTab.Entry] = {}
    with open(self.path) as f:
      for line_number, line in enumerate(f, start=1):
        if line.startswith("#") or not line.strip():
          continue
        split = line.strip("\n").split(LogTab.FS, 3)
        if len(split) != 4:
          errmsg = f"Skipping malformed logtab record at {self.path}:{line_number}: {line.rstrip('\n')}"
          self._value_err(errmsg)
          continue

        ts, operation, key, value = split
        match operation:
          case "set":
            results[key] = LogTab.Entry(ts=ts, value=value)
          case "del":
            results.pop(key, None)
          case "clr":
            to_delete = [k for k in results if k.startswith(key)]
            for k in to_delete:
              results.pop(k)
          case _:
            errmsg = f"Skipping malformed logtab record at {self.path}:{line_number}: {line.rstrip('\n')}"
            self._value_err(errmsg)
            continue

    return results

  def write(self, key: str, value: str):
    """Write a single value to the table"""
    LogTab.write_entry(self.path, key, "set", value)

  def read(self, key: str) -> Entry | None:
    """Read a single entry by exact match. This performs a full file scan."""
    return self.load().get(key)

  def clear(self, prefix: str, comment: str = "") -> None:
    """Clear all values matching the given prefix.
    The optional comment is stored on the line, but never used by the table.
    """
    LogTab.write_entry(self.path, prefix, "clr", comment)

  def delete(self, key: str, comment: str = "") -> None:
    """Delete a single key by exact match.
    The optional comment is stored on the line, but never used by the table.
    """
    if self.strict and key not in self.load():
      raise ValueError(f"Key {key} not found in logtab {self.path} for deletion")
    LogTab.write_entry(self.path, key, "del", comment)

  def history(self, prefix: str = "", suffix: str = "") -> list[tuple[str, Entry]]:
    """Every `set` record matching, oldest first, without collapsing by key.

    `load` answers "what is the value now"; this answers "what happened", which
    is the whole reason the table keeps its history. `del`/`clr` records are
    skipped: they end a value's life, they are not events of their own.
    """
    records: list[tuple[str, LogTab.Entry]] = []
    with open(self.path) as f:
      for line in f:
        if line.startswith("#") or not line.strip():
          continue
        split = line.strip("\n").split(LogTab.FS, 3)
        if len(split) != 4 or split[1] != "set":
          continue
        ts, _, key, value = split
        if key.startswith(prefix) and key.endswith(suffix):
          records.append((key, LogTab.Entry(ts=ts, value=value)))
    return records

  def scan(
    self, prefix: str = "", suffix: str = "", contains: str = ""
  ) -> dict[str, Entry]:
    """Scan the table for entries matching the given prefix and suffix.
    If no prefix or suffix is given, this is the same as load()
    """
    if prefix:
      LogTab.validate_key(prefix)
    if suffix:
      LogTab.validate_key(suffix)
    if contains:
      LogTab.validate_key(contains)

    data = self.load()
    return {
      k: v
      for k, v in data.items()
      if k.startswith(prefix) and k.endswith(suffix) and contains in k
    }
