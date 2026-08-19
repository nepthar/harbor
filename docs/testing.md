# Testing

```bash
uv run pytest
```

Around 200 tests in ~15s. If that number starts climbing, something below has
been violated.

## How it works

Commands run **in this process**. `HarborEnv.run` calls `harbor.cli.main.run`
directly and captures stdout, stderr, and `logging` output; it returns a
`Result` shaped like `subprocess.CompletedProcess`. Spawning an interpreter per
command used to cost ~0.12s and bought nothing — the environment is already
isolated by the `harbor_env` fixture.

Two consequences worth knowing:

- The fixture sets `HARBOR_CONFIG` and `chdir`s to the harbor root, because
  those used to be `subprocess.run` arguments and now have to be real process
  state.
- `HarborEnv.run_subprocess` exists for the one case that needs a genuinely
  separate process (cross-process lock contention). Reach for it only when the
  test would be vacuous otherwise.

**Tests must never reach the real docker daemon.** `tests/conftest.py` shadows
`docker` with a guard that both refuses and *records* the call — recording
matters because harbor calls docker with `check=False` in places, which would
turn a refusal into an empty result that looks like "nothing is running".
`harbor_env` installs a working fake ahead of the guard on `PATH`.

`HARBOR_LOCK_TIMEOUT` overrides the 5s lock acquire timeout; the suite sets it
to 0.25s.

## Layout

| File | Covers |
|---|---|
| `test_logtab.py` | The append-only key-value log |
| `test_routes.py`, `test_ports.py` | Route records and host port allocation |
| `test_config_schema.py` | Config store, encryption, binds, metadata |
| `test_stack.py` | Manifest bytes in, `AppStack` out |
| `test_compose.py` | `AppStack` + run data out to a compose file; readiness |
| `test_fetch.py` | `harbor fetch` against an in-process fake GitHub |
| `test_layout.py` | Staging: the run dir, volume links, re-staging |
| `test_cli.py` | The command surface — exit codes, output, disk state |
| `test_lock.py` | One lock per invocation; who holds it and for how long |
| `test_restore.py` | Snapshot and restore, including data volumes |
| `test_docker.py` | That the docker guard actually fails a stray call |

`test_stack.py` and `test_compose.py` share `stack_of` from `conftest.py`:
manifest TOML in, `AppStack` out, through the real parse-and-validate path.
Reach for it before writing another CLI test — most questions about what a
manifest *means* are answerable in a hundredth of the time.

## Live tests

These are not automated, and deliberately so — each one's difficulty *is* the
thing being tested, and a fake would only assert that the fake works. Run them
by hand against a real harbor root before a release or after touching the
relevant area.

**Snapshot and restore of real volume data.** Containers write their files as
root, so `lifecycle/rootfs.py` does the `tar`, `cp -a` and `rm -rf` in a
throwaway container instead of on the host. `test_restore.py` pins down the
docker command harbor builds, but the fake runs that command's script on the
host as an ordinary user, so the part that actually needs root is untested.
Check: a snapshot of an app with genuinely root-owned files in a data volume,
ownership and modes preserved on the way in, symlinks *inside* a volume not
dereferenced, an archive left owned by the invoking user rather than root, and
a restore that brings the data back intact. Also that an interrupted snapshot
leaves the staging dir with the message that names it, and that harbor pulls
the pinned image on a host that does not have it yet.

**Refusing to run as root.** `refuse_root` is unit-tested against a faked uid;
that it fires for a genuine `sudo harbor …` is not.

**Real `docker compose up` / `down`.** The fake in `conftest.py` knows four
argument shapes. Untested against it: image pull, `${__HARBOR_CONFIG__*}`
interpolation actually reaching the container, restart policies, `depends_on`
ordering, and drift between compose versions.

**A multi-container app.** `unifi-network-application.happ` is the natural
smoke test — two units, a generated secret shared between them, and a real
health dependency.

**Nginx Proxy Manager route provider.** `test_cli.py` stubs the provider and
only pins down *when* harbor calls it. Against a real NPM: token fetch, cache
and expiry, the wildcard certificate lookup, refusing a route another app
already owns, and teardown on `harbor stop`.

**`logs -f`.** Streaming and TTY behavior, and that Ctrl-C exits 130 without a
traceback.

**`harbor init` on a fresh host.** Ownership and permissions of the volume
roots as a non-root user, and on a filesystem that is not the developer's.

**`harbor fetch` against real GitHub.** Rate-limit headers, redirects, and a
large happ. The fake serves the two endpoints harbor uses and nothing else.
