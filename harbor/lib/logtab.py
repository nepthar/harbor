import logging
import os
import re
from collections.abc import Iterator
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
  MAX_KEY_LENGTH = 2048
  MAX_VAL_LENGTH = 2048

  OPERATIONS = set(["set", "del", "clr"])

  @staticmethod
  def ts() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

  @staticmethod
  def validate_key(key: str):
    if (
      not key
      or len(key) > LogTab.MAX_KEY_LENGTH
      or LogTab.KEY_RE.fullmatch(key) is None
    ):
      raise ValueError(f"Invalid logtab key: {key}")

  @staticmethod
  def validate_value(val: str):
    if len(val) > LogTab.MAX_VAL_LENGTH or "\n" in val:
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

  def __init__(
    self,
    path: Path,
    *,
    strict: bool = False,
    title: str = "",
    auto_compact_size_bytes: int = 0,
    auto_compact_history: int = 0,
  ):
    self.strict = strict
    self.path = path
    self._compact_size = auto_compact_size_bytes
    self._compact_history = auto_compact_history
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

  def _records(self) -> Iterator[list[str]]:
    """Yield valid rows as a split."""
    with open(self.path) as f:
      for line_number, line in enumerate(f, start=1):
        if line.startswith("#") or not line.strip():
          continue
        split = line.strip("\n").split(LogTab.FS, 3)
        if len(split) != 4 or split[1] not in LogTab.OPERATIONS:
          errmsg = (
            f"Skipping malformed logtab record at {self.path}:{line_number}: "
            f"{line.rstrip('\n')}"
          )
          self._value_err(errmsg)
          continue
        yield split

  def load(self) -> dict[str, Entry]:
    """Materialize the table into a dictionary of Entries."""
    results: dict[str, LogTab.Entry] = {}
    for ts, operation, key, value in self._records():
      match operation:
        case "set":
          results[key] = LogTab.Entry(ts=ts, value=value)
        case "del":
          results.pop(key, None)
        case "clr":
          to_delete = [k for k in results if k.startswith(key)]
          for k in to_delete:
            results.pop(k)
    return results

  def _maybe_compact(self) -> None:
    if self._compact_size > 0 and self.path.stat().st_size > self._compact_size:
      self.compact(self._compact_history)

  def write(self, key: str, value: str):
    """Write a single value to the table"""
    LogTab.write_entry(self.path, key, "set", value)
    self._maybe_compact()

  def read(self, key: str) -> Entry | None:
    """Read a single entry by exact match. This performs a full file scan."""
    return self.load().get(key)

  def clear(self, prefix: str, comment: str = "") -> None:
    """Clear all values matching the given prefix.
    The optional comment is stored on the line, but never used by the table.
    """
    LogTab.write_entry(self.path, prefix, "clr", comment)
    self._maybe_compact()

  def delete(self, key: str, comment: str = "") -> None:
    """Delete a single key by exact match.
    The optional comment is stored on the line, but never used by the table.
    """
    if self.strict and key not in self.load():
      raise ValueError(f"Key {key} not found in logtab {self.path} for deletion")
    LogTab.write_entry(self.path, key, "del", comment)
    self._maybe_compact()

  def history(self, prefix: str = "", suffix: str = "") -> list[tuple[str, Entry]]:
    """Every `set` record matching, oldest first, without collapsing by key.

    `load` answers "what is the value now"; this answers "what happened", which
    is the whole reason the table keeps its history. `del`/`clr` records are
    skipped: they end a value's life, they are not events of their own.
    """

    if prefix:
      LogTab.validate_key(prefix)
    if suffix:
      LogTab.validate_key(suffix)

    records: list[tuple[str, LogTab.Entry]] = []
    for ts, operation, key, value in self._records():
      if operation != "set":
        continue
      if key.startswith(prefix) and key.endswith(suffix):
        records.append((key, LogTab.Entry(ts=ts, value=value)))
    return records

  def compact(self, history: int = 0) -> None:
    """Rewrite this file to current state plus the last ``history`` records.

    Backfill is one ``set`` per live key whose exact key is not in that tail,
    oldest timestamp first, then the tail in file order, then
    ``# end compacted entries``. The live path is replaced; the previous
    bytes are discarded. The caller must keep writers off the file.

    Does nothing when the file has fewer data records than ``history``.
    """
    if history < 0:
      raise ValueError(f"history must be >= 0, got {history}")

    records = list(self._records())
    current: dict[str, LogTab.Entry] = {}
    for ts, operation, key, value in records:
      match operation:
        case "set":
          current[key] = LogTab.Entry(ts=ts, value=value)
        case "del":
          current.pop(key, None)
        case "clr":
          to_delete = [k for k in current if k.startswith(key)]
          for k in to_delete:
            current.pop(k)

    if history > 0 and len(records) < history:
      return

    suffix = records[-history:] if history else []
    suffix_keys = {key for _, _, key, _ in suffix}
    backfill = [
      (entry.ts, "set", key, entry.value)
      for key, entry in current.items()
      if key not in suffix_keys
    ]
    backfill.sort(key=lambda rec: (rec[0], rec[2]))

    compact_path = self.path.with_name(self.path.name + ".compact")
    lines = [
      f"# Compacted logtab at {LogTab.ts()}",
      "# Format: <date>\\t<set|del|clr>\\t<key>\\t<value>\\n",
    ]
    for rec in backfill:
      lines.append(LogTab.FS.join(rec))
    for rec in suffix:
      lines.append(LogTab.FS.join(rec))
    lines.append("# end compacted entries")
    data = ("\n".join(lines) + "\n").encode()

    fd = os.open(compact_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
      offset = 0
      while offset < len(data):
        offset += os.write(fd, data[offset:])
      os.fsync(fd)
    finally:
      os.close(fd)

    os.replace(compact_path, self.path)
    compact_path.unlink(missing_ok=True)

  def scan(self, prefix: str = "", suffix: str = "") -> dict[str, Entry]:
    """Scan the table for entries matching the given prefix and suffix.
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
