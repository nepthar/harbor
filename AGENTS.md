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

## Conventions

- ruff: 88 cols, **2-space indent**, double quotes, py312.
- Comments explain *why*, and never restate the code.
- Errors are `ValueError` / `RuntimeError` whose message names the fix.
- Tests must never reach the real docker daemon — `tests/conftest.py` enforces
  this. Test doubles live in `tests/`, never in `harbor/`.
- The suite runs in ~15s and commands run in-process; see `docs/testing.md`
  before adding a test that spawns a subprocess or waits on a timeout. What
  cannot be tested without a real daemon is a live test, listed in that doc.
- Run `uv run ruff check harbor tests`, `uv run ruff format --check harbor tests`,
  and `uv run pytest` before reporting done. Don't report unrun tests as passing.
