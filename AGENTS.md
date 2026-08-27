# Harbor

Container stack management for self-hosters. **Pre-beta: one operator, no users
to migrate, no backwards compatibility.** Delete old code paths rather than
deprecating them. Don't write migration or compatibility code unless asked.

## How to work here

**Smallest thing that works, first.** A function before a module, a module
before a package. Build the happy path, then wait for a real problem before
hardening. Don't insure against events that haven't happened yet.

**Refuse, don't orchestrate.** When a precondition isn't met, raise an error
naming the command that fixes it — don't stop-and-restart, retry, or otherwise
work around it. This is the largest single source of accidental complexity in
this codebase.

**No design docs unless asked.** A doc written before the code becomes a scope
floor: every table wants filling, every decision log wants decisions, and a
subagent will implement all of it. When asked for one, it describes what exists.

**Question the premise.** If I ask something that presupposes a design ("what
should the index format be?"), say so and offer the version without it before
answering. My questions can ratify complexity I don't actually want.

**After a structural change, say what it obsoleted.** A layout change can make
an earlier decision pointless. Name it instead of building on top of it.

**Delegating to subagents:** constrain by shape — this file, this signature,
this size. Handing over a document produces a document's worth of code.


** TESTS ** Only run focused tests, don't bother running the full test suite for cosmetic changes.


## Conventions

- ruff: 88 cols, **2-space indent**, double quotes, py312.
- **Comments: only what the code can't say.** Default to none. Write one only
  when its absence would let a competent reader introduce a bug — a non-local
  constraint (the caller holds this lock, this must run before X), surprising
  external behavior (docker recreates a missing bind source as root), or a
  deliberate deviation that reads as a mistake. Never to explain a design
  choice, justify wording, record an alternative you rejected, or restate a
  name. If the comment is longer than the code under it, it's the wrong
  comment. Existing files are more heavily commented than I now want — do not
  match their density.
- **Docstrings: one line.** A second only for a non-obvious contract (what it
  raises, what it assumes). No rationale.
- Errors are `ValueError` / `RuntimeError` whose message names the fix.
- Tests must never reach the real docker daemon — `tests/conftest.py` enforces
  this. Test doubles live in `tests/`, never in `harbor/`.
- The suite runs in ~15s and commands run in-process; see `docs/testing.md`
  before adding a test that spawns a subprocess or waits on a timeout. What
  cannot be tested without a real daemon is a live test, listed in that doc.
- Run `uv run ruff check harbor tests`, `uv run ruff format --check harbor tests`,
  and `uv run pytest` before reporting done. Don't report unrun tests as passing.
