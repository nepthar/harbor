"""Tests for LogTab, the append-only key/value log backing the harbor store.

LogTab is a stable, low-level component; these tests pin down its full contract:
round-trips, prefix/suffix scans, prefix-clear vs exact-delete, append-only
persistence across reopens, header handling, key/value validation, and tolerance
of comment/blank lines.
"""

import logging
import os

import pytest

from harbor.lib.logtab import LogTab


@pytest.fixture
def tab(tmp_path):
  return LogTab(tmp_path / "t.logtab")


def _values(entries: dict[str, LogTab.Entry]) -> dict[str, str]:
  return {k: e.value for k, e in entries.items()}


def test_timestamp_uses_utc_z_suffix():
  assert LogTab.ts().endswith("Z")


def test_entry_carries_timestamp(tab):
  tab.write("a", "1")
  entry = tab.read("a")
  assert entry is not None
  assert entry.value == "1"
  assert entry.ts.endswith("Z")
  assert entry.datetime().tzinfo is not None


# ── write / read / overwrite ──────────────────────────────────────────────
def test_write_then_read(tab):
  tab.write("a", "1")
  assert tab.read("a").value == "1"


def test_write_uses_atomic_append_and_retries_short_write(
  tmp_path, monkeypatch, caplog
):
  path = tmp_path / "t.logtab"
  path.touch()
  tab = LogTab(path)
  real_open = os.open
  real_write = os.write
  opened_with = []
  requested = []

  def recording_open(path, flags, mode):
    opened_with.append(flags)
    return real_open(path, flags, mode)

  def short_first_write(fd, data):
    requested.append(len(data))
    size = 4 if len(requested) == 1 else len(data)
    return real_write(fd, data[:size])

  monkeypatch.setattr("harbor.lib.logtab.os.open", recording_open)
  monkeypatch.setattr("harbor.lib.logtab.os.write", short_first_write)

  with caplog.at_level(logging.ERROR):
    tab.write("a", "value")

  assert opened_with[0] & os.O_APPEND
  assert requested[1] == requested[0] - 4
  assert tab.read("a").value == "value"
  assert "did not happen atomically" in caplog.text


def test_read_missing_key_is_none(tab):
  assert tab.read("nope") is None


def test_last_write_wins(tab):
  tab.write("a", "1")
  tab.write("a", "2")
  assert tab.read("a").value == "2"
  assert _values(tab.load()) == {"a": "2"}


def test_load_returns_all_live_keys(tab):
  tab.write("a", "1")
  tab.write("b", "2")
  assert _values(tab.load()) == {"a": "1", "b": "2"}


# ── scan ──────────────────────────────────────────────────────────────────
def test_scan_by_prefix(tab):
  tab.write("apps/x/config/u", "1")
  tab.write("apps/x/config/p", "2")
  tab.write("system/secrets/s", "3")
  assert _values(tab.scan("apps/x/")) == {
    "apps/x/config/u": "1",
    "apps/x/config/p": "2",
  }


def test_scan_by_suffix(tab):
  tab.write("routes/x/main", "41000")
  tab.write("routes/y/main", "41001")
  tab.write("apps/x/config/u", "alice")
  assert set(tab.scan(suffix="/main")) == {
    "routes/x/main",
    "routes/y/main",
  }


def test_scan_empty_prefix_returns_everything(tab):
  tab.write("a", "1")
  tab.write("b", "2")
  assert _values(tab.scan("")) == {"a": "1", "b": "2"}


# ── clear (prefix) vs delete (exact) ──────────────────────────────────────
def test_clear_removes_matching_prefix(tab):
  tab.write("a/1", "x")
  tab.write("a/2", "y")
  tab.write("b/1", "z")
  tab.clear("a/")
  assert _values(tab.load()) == {"b/1": "z"}


def test_clear_no_match_is_noop(tab):
  tab.write("a", "1")
  tab.clear("zzz/")
  assert tab.read("a").value == "1"


def test_clear_is_prefix_based_not_exact(tab):
  # clear matches by prefix: "app" would also match "apple".
  tab.write("app", "1")
  tab.write("apple", "2")
  tab.clear("app")
  assert tab.load() == {}


def test_delete_is_exact_not_prefix(tab):
  # delete removes only the exact key, leaving prefix-siblings intact.
  tab.write("app", "1")
  tab.write("apple", "2")
  tab.delete("app")
  assert _values(tab.load()) == {"apple": "2"}


def test_delete_missing_key_is_noop(tab):
  tab.write("a", "1")
  tab.delete("missing")
  assert tab.read("a").value == "1"


def test_delete_then_read_is_none(tab):
  tab.write("a", "1")
  tab.delete("a")
  assert tab.read("a") is None


def test_rewrite_after_delete(tab):
  tab.write("a", "1")
  tab.delete("a")
  tab.write("a", "2")
  assert tab.read("a").value == "2"


# ── append-only persistence across reopen ─────────────────────────────────
def test_state_persists_across_instances(tmp_path):
  path = tmp_path / "t.logtab"
  first = LogTab(path)
  first.write("a", "1")
  first.write("b", "2")
  first.delete("b")

  reopened = LogTab(path)
  assert reopened.read("a").value == "1"
  assert reopened.read("b") is None
  assert _values(reopened.load()) == {"a": "1"}


def test_reopen_does_not_rewrite_header(tmp_path):
  path = tmp_path / "t.logtab"
  LogTab(path).write("a", "1")
  before = path.read_text()
  # Reopening an existing file must not append another header.
  LogTab(path)
  assert path.read_text() == before
  assert before.count("# Format:") == 1


def test_history_is_append_only(tmp_path):
  # Every mutation is a new line; nothing is rewritten in place.
  path = tmp_path / "t.logtab"
  tab = LogTab(path)
  tab.write("a", "1")
  tab.write("a", "2")
  tab.delete("a")
  body = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
  assert len(body) == 3
  assert body[0].split("\t")[1:3] == ["set", "a"]
  assert body[1].split("\t")[1:3] == ["set", "a"]
  assert body[2].split("\t")[1:3] == ["del", "a"]


# ── header / comment / blank-line tolerance ───────────────────────────────
def test_fresh_file_loads_clean(tmp_path):
  # Regression: the created header must not leave a stray blank line that
  # breaks load().
  path = tmp_path / "t.logtab"
  LogTab(path)  # creates header only
  assert LogTab(path).load() == {}


def test_title_written_into_header(tmp_path):
  path = tmp_path / "t.logtab"
  LogTab(path, title="my table")
  assert "# my table" in path.read_text()


def test_load_ignores_comments_and_blank_lines(tmp_path):
  # A file created by older/buggy writers may contain blank lines; load must
  # skip comments and blanks rather than raising.
  path = tmp_path / "legacy.logtab"
  path.write_text(
    "# header\n"
    "\n"
    "# another comment\n"
    "2026-01-01T00:00:00-00:00\tset\ta\t1\n"
    "\n"
    "2026-01-01T00:00:01-00:00\tset\tb\t2\n"
  )
  assert _values(LogTab(path).load()) == {"a": "1", "b": "2"}


def test_load_skips_malformed_line(tmp_path, caplog):
  path = tmp_path / "bad.logtab"
  path.write_text(
    "# header\n"
    "2026-01-01T00:00:00-00:00\tset\ta\t1\n"
    "this-is-not-a-valid-record\n"
    "2026-01-01T00:00:01-00:00\tset\tb\t2\n"
  )

  assert _values(LogTab(path).load()) == {"a": "1", "b": "2"}
  assert "Skipping malformed logtab record" in caplog.text


# ── key / value validation ────────────────────────────────────────────────
@pytest.mark.parametrize("key", ["", "has space", "bad!char", "x" * 513])
def test_write_rejects_invalid_key(tab, key):
  with pytest.raises(ValueError):
    tab.write(key, "v")


@pytest.mark.parametrize("key", ["a", "A9", "apps/io.p2net.x/config/name", "a-b_c.d"])
def test_write_accepts_valid_keys(tab, key):
  tab.write(key, "v")
  assert tab.read(key).value == "v"


def test_write_rejects_newline_in_value(tab):
  with pytest.raises(ValueError):
    tab.write("a", "line1\nline2")


def test_write_rejects_oversized_value(tab):
  with pytest.raises(ValueError):
    tab.write("a", "x" * 1025)


def test_value_may_contain_tabs(tab):
  # The field separator is a tab, but values with tabs still round-trip
  # because load() splits with a bounded maxsplit.
  tab.write("a", "one\ttwo\tthree")
  assert tab.read("a").value == "one\ttwo\tthree"


def test_empty_value_round_trips(tab):
  tab.write("a", "")
  assert tab.read("a").value == ""
  assert _values(tab.load()) == {"a": ""}
