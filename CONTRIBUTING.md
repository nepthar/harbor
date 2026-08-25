# Contributing to Harbor

Harbor is licensed under the [Apache License 2.0](LICENSE). Contributions are
accepted under that same license, and you keep the copyright to your work.

## Sign off your commits (DCO)

Harbor uses the [Developer Certificate of Origin](DCO) instead of a CLA. There
is nothing to sign and no account to create — you just add a `Signed-off-by`
line to each commit, certifying that you wrote the change (or otherwise have
the right to submit it under Apache-2.0). The full text is in [DCO](DCO); the
four clauses are what your sign-off attests to.

Git adds the line for you:

```bash
git commit -s
```

which appends a trailer built from your `user.name` and `user.email`:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Use a real name and a reachable email. Forgot it on the last commit? Amend with
`git commit --amend -s`; for a whole branch, `git rebase --signoff main`.

## Before you open a PR

- Read [AGENTS.md](AGENTS.md) — it is the house style, and it is short.
- `uv run pytest` passes (the `docker`-marked tests need a real daemon and are
  deselected by default).
- `uv run ruff check` and `uv run ruff format --check` are clean.
