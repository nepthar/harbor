# Run layout, app lifecycle, and snapshots — Engineering Design

Status: **approved for implementation.**

Supersedes the previous version of this document, and supersedes
`docs/snapshots.md` on `jp/snapshots` (whose D1–D15 are folded in or overturned
here; see §14). These were three designs; the run layout turned out to be the
organizing idea and the other two fall out of it, so they are one document now.

---

## 1. What changes and why

Today a staged app does not own anything. `run/<app_id>/source` is a **symlink**
to wherever the `.happ` happens to live, config lives in a central `harbordb`,
and `app`-kind volumes point back out at the original directory. Three
consequences, all observed on a real install:

1. **"What is running?" has no answer.** Editing a manifest in an editor
   silently changes what the installed app *is*.
2. **A container can write into your source tree**, because `app` volumes
   resolve to `<bundle>/<src>` and are only read-only if the manifest says so.
3. **Deleting the source bricks the app**, even though harbor has a perfectly
   good `manifest.toml` copy in the run dir.

The fix is to make `run/<app_id>/` a **self-contained, snapshottable unit**: the
happ, its config, its volume links, its compose file. Once that is true, a
snapshot is very nearly "tar this directory", `rm` is "delete this directory",
and the special cases that currently exist to chase symlinks around the
filesystem all disappear.

### Non-goals

- `harbor update` — the name is reserved, the behavior is deferred (§13).
- Semantic manifest diffing. Deferred with `update`.
- Portable snapshots. Explicitly rejected: see L12.
- A dev/non-dev app distinction. Everything in `apps/` is treated as a folder;
  if you symlink one in, you are fooling the system and that is fine.

---

## 2. Decision log

| # | Decision | Rationale |
| --- | --- | --- |
| L1 | `stage` **copies the whole `.happ` into `run/<app_id>/happ/`**, always re-copying. | Makes "what is installed" a fact on disk instead of a pointer to something that may have changed since. |
| L2 | **`apps/` is the catalog** and the only source `stage` copies from. Entries may be real folders or symlinks; harbor does not distinguish. | One resolution path. A developer symlinks their checkout into `apps/` and re-runs `harbor stage` after each edit. |
| L3 | Volume links are **typed**: `run/<id>/volumes/<kind>/<name>`. | Makes snapshot selection a path glob (`volumes/data/*`) rather than a manifest lookup, and shows at a glance which volumes are durable. |
| L4 | `app` volumes are **always read-only**, and `readonly = false` on one is a manifest error. | The happ is input, never state. Enforced rather than true-by-convention, so writes fail at mount time instead of being silently discarded by the next `stage`. |
| L5 | Per-app config — params, secrets, binds — moves to **`run/<app_id>/config.logtab`**. Secrets stay **Fernet-encrypted** under the master key, exactly as today. | Makes the run dir the whole unit, so a snapshot needs no separate state extraction. Not a mirror of harbordb — a move; there is still exactly one writer. |
| L6 | **harbordb keeps** routes, host-port allocation, and system secrets, and is not renamed. | The rule is: per-app state lives in the run dir; anything involving *contention between apps* stays central. Port allocation is the reason routes stay put. |
| L7 | CLI becomes **`stage` / `start` / `stop`**. `up` and `down` are removed. | `stage` = deploy, `start`/`stop` = lifecycle. Each verb means one thing. `start` on an unstaged app stages first, so the common case is still one command. |
| L8 | **One removal command.** `harbor rm <app>` snapshots, then deletes the run dir, all managed volumes, and route entries. `--no-snapshot` skips the snapshot. | Two flags that differed subtly was worse than one that is always complete and always recoverable. "Reset an app" is `harbor rm <app>; harbor start <app>`. |
| L9 | A snapshot is a **tar of `./run/<app_id>` + `./volumes/data/<app_id>` + `./snapshot.toml`**. | Falls straight out of L1/L3/L5. Config, manifest, and compose come along because they are already in the run dir. |
| L10 | tar runs under **`sudo`**, not in a root container. | A container with `-v /:/host` is root-equivalent anyway — the container laundered privilege rather than avoiding it. sudo is honest, needs no pre-pulled image, and **works when the docker daemon is down**, which is exactly when you are restoring. |
| L11 | Archive is built **uncompressed**, with compression as an optional final pass. | `-r` (append) is how the second root gets in without a scratch copy, and it only works on uncompressed archives. Plain tar also means `tar xf` with no flags, which suits the no-lock-in promise. |
| L12 | **Snapshots are not portable.** Secrets stay Fernet ciphertext; the archive records a master-key fingerprint and rollback refuses on mismatch. | Restoring anywhere requires the same `master.key`. The fingerprint is what turns that into a clear error instead of a Fernet traceback. |
| L13 | **Bare restore is supported and documented**: rolling back an app that no longer exists on this harbor. | L8 makes it load-bearing — undoing an `rm` is only possible this way. |
| L14 | *(Proposal beyond what was specified — revert if you disagree.)* `harbor stage <path>` **symlinks the path into `apps/` first**, then stages from there. | Keeps the dev affordance while making L2 literally true: `apps/` is always the source. It also deletes `_staged_sources`, the `source` symlink, and the dual-source resolution in `bundle_path`. |

---

## 3. Target layout

```
$harbor/
  apps/<app_id>.happ/              catalog: fetch target. Folder or symlink; harbor does not care.

  run/<app_id>/
    happ/                          harbor's copy of the .happ
      manifest.toml
      <everything else>
    config.logtab                  params, secrets (Fernet), binds, origin
    compose.yml                    generated
    volumes/
      app/<name>   -> ../../happ/<src>                        relative; mounted :ro
      data/<name>  -> <volume_roots.data>/<app_id>/<name>     absolute
      temp/<name>  -> …                                       absolute
      logs/<name>  -> …                                       absolute
      bulk/<name>  -> …                                       absolute
      ext/<name>   -> <bound host path>                       absolute

  harbordb.logtab                  routes, port allocations, system secrets
  activity.logtab                  per-app status + harbor/lock records
  snapshots/<app_id>/<ts>[-<label>].happsnap
```

Compose mounts read uniformly as `./volumes/<kind>/<name>:<guest_path>`, with
`:ro` on every `app` volume and on read-only `ext` volumes.

**`app` links are relative, managed links are absolute**, and that asymmetry is
load-bearing. Relative links point inside the run dir, so they tar and restore
correctly anywhere. Absolute links point at `volume_roots`, which may be on
another disk, so they are only meaningful on the machine that made them — which
is why restore **regenerates** links rather than trusting the archived ones
(§9.2). Do not "fix" this by making managed links relative; `volume_roots` is
configurable precisely so it can live elsewhere.

### 3.1 `config.logtab`

A `LogTab` like every other, so it is append-only, human-readable, and carries
its own change history.

```
config/<name>   {"secret": true, "value": "<fernet ciphertext>"}
config/<name>   {"secret": false, "value": "plaintext"}
binds/<name>    {"host_path": "/mnt/nas/media", "readonly": true}
meta/origin     /Users/x/harbor/apps/com.example.unifi.happ
meta/staged_at  2026-07-30T09:14:02-06:00
```

Encryption is unchanged from today: `secret = true` values are Fernet
ciphertext under the master key, and harbor decrypts only when a caller asks for
the value. **Snapshot and restore never decrypt** — they move ciphertext
verbatim, so no plaintext secret ever transits the snapshot code path.

Because config now lives in the run dir, **an app must be staged before it can
be configured.** That is an accepted trade. The flow is:

```
harbor stage foo
harbor config foo --set admin_user=alice
harbor start foo
```

and `harbor start foo --set k=v --bind vol=/path` remains as the one-shot.

---

## 4. Command surface

| Command | Behavior |
| --- | --- |
| `harbor stage APP` | Copy `apps/<id>.happ` → `run/<id>/happ/`, regenerate volume links and `compose.yml`. **Preserves `config.logtab` and `volumes/`.** Refuses if containers are running. |
| `harbor stage PATH` | Symlink `PATH` into `apps/` (L14), then as above. Refuses if an `apps/` entry already exists pointing elsewhere. |
| `harbor start APP [--set K=V] [--bind V=PATH]` | If not staged, stage first. Apply `--set`/`--bind`, check blockers, `compose up -d`, register web routes. |
| `harbor stop APP` | Unregister web routes, `compose down`. |
| `harbor rm APP [--no-snapshot] [-y]` | §8. Snapshot, stop, delete run dir + managed volumes + route entries. |
| `harbor snapshot APP [--label L]` | §7. |
| `harbor snapshot list\|inspect\|prune` | Unchanged in spirit from the previous design. |
| `harbor rollback APP [REF] [--no-pre-snapshot] [-y]` | §9. |
| `harbor update` | **Reserved. Not implemented** (§13). |

`up` and `down` are gone. This is a wide rename: README quickstart, every e2e
test, and `harbor init`'s printed next-steps all reference them.

---

## 5. `stage`

1. Resolve the app id. For a path argument, create `apps/<app_id>.happ` as a
   symlink to it (L14) and say so on stdout; refuse if an entry already exists
   pointing somewhere else.
2. Refuse if any Harbor-labeled container for the app is running, naming
   `harbor stop`.
3. Parse and validate the manifest **from `apps/<id>.happ`**, including the new
   `app`-volume read-only rule (L4).
4. Copy `apps/<id>.happ` → `run/<id>/.happ.incoming`, then swap into `happ/`
   with `os.replace` and delete the old copy. Never edit `happ/` in place: a
   failed copy must not leave a half-updated bundle.
5. Ensure `config.logtab` exists; generate **only the missing** defaults and
   `auto` secrets into it. **Never clear it** — re-staging must not regenerate a
   secret the app's data already depends on. A manifest that adds a new config
   key gets that one key generated; everything already set is untouched.

   Guard: if `config.logtab` is **absent** while managed volume dirs exist and
   are non-empty, **refuse**. That combination means someone deleted the run dir
   by hand, and staging would generate fresh `auto` secrets against data that
   still expects the old ones — an app that fails to authenticate for reasons
   nothing in the error explains. Tell the operator to roll back a snapshot, or
   to `harbor rm` so config and data are deleted together.
6. Rebuild `volumes/` links from the manifest: `app` relative, managed and `ext`
   absolute. Create managed volume dirs that do not exist. Remove links for
   volumes the manifest no longer declares (the link only — never the data).
7. Clear and reallocate routes in harbordb.
8. Write `compose.yml`.
9. Record `meta/origin` and `meta/staged_at`.

"Always re-copies" means steps 4 and 8 — **not** the run dir wholesale. Config
and volume contents survive every stage.

---

## 6. Read-only `app` volumes

- Every `app` volume mounts `:ro`, regardless of the manifest.
- `readonly = false` on an `app` volume is a **manifest validation error**
  naming the volume, not a silently ignored field. An author who wrote it meant
  something, and they should be told it is impossible rather than have it
  quietly reversed.
- `VolumeEntry.readonly` still applies to `ext` volumes.
- Existing happs are unaffected: `demo-routes` already sets it; `demo-volumes`
  sets nothing and only reads.

---

## 7. Snapshot

### 7.1 Archive

```
./snapshot.toml
./run/<app_id>/…              verbatim; symlinks preserved as symlinks
./volumes/data/<app_id>/…     real contents
```

Only `data` volumes are captured. `temp` is disposable, `logs` and `bulk` are
too large to be worth it, `ext` is not ours to copy, and `app` contents are
already inside `run/<id>/happ/`. Everything skipped is recorded in
`snapshot.toml` with a reason.

### 7.2 `snapshot.toml`

Most of the old `index.json` is gone, because it is now implicit in the archive
— config is in `run/<id>/config.logtab`, the manifest is in `run/<id>/happ/`,
the volume set is a directory listing. What remains is what cannot be derived:

```toml
happsnap_version = 1
app_id           = "com.example.unifi"
created_at       = "2026-07-30T01:27:28+00:00"
label            = "pre-rm"      # or prerollback, a user label, or ""
trigger          = "rm"          # manual | rm | rollback
harbor_version   = "0.1.0"
app_version      = "10.4.57"
was_running      = true

[encryption]
scheme                 = "fernet-master-key-v1"
master_key_fingerprint = "82650a5418f4e8a0"

[[volumes]]
name = "db_data"
kind = "data"
captured = true
bytes = 214958080

[[volumes]]
name = "media"
kind = "ext"
captured = false
reason = "ext volume contents are not captured"
host_path = "/mnt/nas/media"
```

`master_key_fingerprint` is the first 16 hex chars of
`sha256(b"harbor-snapshot-fp:" + master_key)`. It exists so a mismatched restore
fails with *"this snapshot was taken with a different master key"* rather than a
Fernet `InvalidToken` (L12).

The archive's own sha256 goes in a `.sha256` sidecar, since it cannot be inside
itself.

### 7.3 Building it

One `sudo sh -c` so the operator sees **one** password prompt:

```sh
tar -cf  $OUT --numeric-owner --sparse -C $STAGING snapshot.toml
tar -rf  $OUT --numeric-owner --sparse -C $HARBOR_ROOT run/$APP_ID
tar -rf  $OUT --numeric-owner --sparse -C $DATA_ROOT \
         --transform 's,^,volumes/data/,' $APP_ID
```

Details that matter:

- **Never pass `--one-file-system`.** tar crosses mount boundaries by default, which is what we want; that flag would silently produce an archive of empty directories.
- **Never pass `-h`/`--dereference`.** It is global, so it cannot be aimed at just the volume links, and it would rewrite symlinks *inside* volume data as duplicate files — a silent corruption of app state. The two roots exist precisely so no dereferencing is needed.
- **`--transform` must not carry the `s` flag** (symlink-target scope). Default scope rewrites member names only; adding `s` would rewrite link targets inside restored volume data. There is a test for this.
- `--transform` is **GNU tar**. macOS ships bsdtar, which spells it `-s`. Check `tar --version` up front and fail with a clear message; document `brew install gnu-tar` for macOS development.
- Each `-r` invocation carries its own `--transform`, so there is no cross-matching hazard between the run segment and the volume segment.
- Write to `<name>.happsnap.partial`, verify, then `os.replace` into place. A `.partial` is unambiguously a crashed run and is never resolvable as a snapshot.
- Compression, if requested, is a final `gzip` pass over the finished archive. Peak disk during that pass is uncompressed + compressed; the free-space precheck accounts for it. Keep the `.happsnap` extension either way — GNU tar and `tarfile.open(path, "r:*")` both auto-detect, so there is no format fork.

### 7.4 Sequence

1. Resolve the app; read run state; `was_running = running_count > 0`.
2. **Preflight — everything that can fail must fail before the app is stopped:** GNU tar present; free space (data volume size × 1.2, plus the compressed size if compressing); `snapshots_root` writable and mode 0700.
3. If running, `harbor stop` (unregister routes, `compose down --timeout N`).
4. Write `snapshot.toml` to a staging dir.
5. Build the archive (§7.3), compute sha256 while doing so, publish atomically, `chmod 0600`, write the sidecar.
6. If `was_running`, start again. **If that start fails, the snapshot is still reported as a success** — it is durable on disk — and the start failure is reported separately with a non-zero exit. Never lose a good snapshot to an unrelated start failure.
7. Record activity.

---

## 8. `rm`

```
harbor rm APP [--no-snapshot] [-y]
```

1. Resolve; confirm unless `-y`, listing exactly what will be deleted.
2. Unless `--no-snapshot`: capture with `label = "pre-rm"`, `trigger = "rm"`, **and verify its checksum**.
3. Stop the app if running.
4. Delete `run/<app_id>/`.
5. Delete `volume_roots[kind]/<app_id>` for every managed kind.
6. Clear `routes/<app_id>/*` from harbordb.
7. Record activity `removed`.

**Ordering is safety-critical.** The snapshot must be written, fsynced, and
checksum-verified before the first byte is deleted; any failure aborts with
nothing removed. Otherwise a disk-full mid-snapshot turns `rm` into
unrecoverable deletion.

What survives on purpose: `apps/<app_id>.happ` (the catalog entry, so
`harbor rm foo; harbor start foo` is a clean reinstall), `snapshots/<app_id>/`
(the safety net), and any `ext` volume contents (not ours — report them).

---

## 9. Rollback

### 9.1 Phase A — read-only, refuses loudly

Verify the sidecar checksum; read `snapshot.toml`; refuse on
`happsnap_version` newer than known, on `app_id` mismatch, or on
**master-key fingerprint mismatch** (L12). Check GNU tar and free space. Then
print the plan and confirm unless `-y`.

### 9.2 Phase B/C — destructive

1. Unless `--no-pre-snapshot`, capture with `label = "prerollback"`. Print its path; every subsequent error references it.
2. Stop the app if running.
3. Wipe and extract, as root, in one `sudo sh -c`:
   ```sh
   rm -rf $HARBOR_ROOT/run/$APP_ID
   tar -xf $A --numeric-owner --same-owner -p -C $HARBOR_ROOT run/$APP_ID
   find $DATA_ROOT/$APP_ID -mindepth 1 -delete || true
   [ -z "$(ls -A $DATA_ROOT/$APP_ID)" ] || { echo "wipe incomplete" >&2; exit 1; }
   tar -xf $A --numeric-owner --same-owner -p -C $DATA_ROOT \
       --strip-components=2 volumes/data/$APP_ID
   ```
   The emptiness check gates extraction rather than `find`'s exit status: BSD
   `find` returns 0 even when `-delete` hits `EPERM`, and survivors from a newer
   version merged into restored data is exactly the corruption the wipe exists
   to prevent.
4. **Regenerate `volumes/` links and `compose.yml` from the restored `happ/`.** Do not trust the archived symlinks — the managed ones are absolute and only valid on the machine that made them (§3).
5. Reallocate routes (they are disposable; this cannot collide with an app that claimed the old port, because the global lock is held throughout).
6. If `apps/<app_id>.happ` does not exist, materialize it from the restored `happ/` — so a bare restore leaves an app that can also be re-staged. **Never overwrite an existing catalog entry.**
7. Start if `was_running`.

Any Phase C failure must name the pre-rollback snapshot and the exact command to
return to it.

### 9.3 Bare restore (L13)

Rolling back an app with no run dir, no catalog entry, and no harbordb state
works: the archive carries the happ, the config, and the data. Ref resolution
must therefore fall back to scanning `snapshots_root` when `resolve_app` finds
nothing.

**This is the only way to undo `harbor rm`,** which is why it is supported
rather than merely possible. Document it with one loud caveat: a snapshot is
useless without the matching `master.key` (L12). Anyone moving to a new machine
must copy `master.key` too, and `harbor snapshot inspect` should show the
fingerprint so that is checkable before the move, not after.

---

## 10. Privilege model

- **If already root, never escalate.** The future `harbord` will run as a system service and will never prompt.
- **Otherwise use sudo**, unconditionally, for the tar create/extract step. Not a readability probe: predictable beats clever for a backup tool, and a probe that says "probably readable" is worse than useless.
- Print **one line explaining why** before the prompt: the archive must preserve the uids the app's files are owned by, and restoring a database directory as the wrong user produces something that unpacks fine and never starts again.
- `sudo -n true` first — with cached credentials or a NOPASSWD rule there is no prompt at all.
- **If no TTY and sudo needs a password, fail with instructions** rather than hanging on a prompt nothing will answer.
- `--no-sudo` runs unprivileged and fails loudly if it cannot read; for the case where the operator knows every file is theirs.
- Document the scoped sudoers snippet for unattended use, and say plainly what it grants.

Known limitation, unchanged: on macOS Docker Desktop the bind-mount layer
squashes ownership, so uid/gid preservation cannot be exercised there. Volumes
on NFS with root-squash cannot preserve ownership either — detect and say so
rather than writing a quietly broken archive.

---

## 11. Edge cases

| Situation | Behavior |
| --- | --- |
| `stage` on a running app | Refuse, name `harbor stop` |
| `stage` twice | Re-copies `happ/`, regenerates compose; config and volume data untouched |
| `stage PATH` when `apps/<id>.happ` exists pointing elsewhere | Refuse; the catalog entry is the source of truth |
| Manifest drops a volume | Link removed, **data left on disk**, reported. Never delete data on a stage |
| Manifest adds a volume | Created empty |
| Manifest changes a volume's *kind* | **Refuse.** The bytes live under the old kind's root; moving them silently is worse than failing |
| `app` volume with `readonly = false` | Manifest validation error naming the volume |
| Config for an unstaged app | Error: stage first. `harbor start --set` covers the one-shot case |
| Run dir deleted by hand, volumes still populated | `stage` **refuses** (§5 step 5). Deleting config and data together is `harbor rm`; recovering config alone is a rollback |
| Run dir and volumes both gone (`rm`, or a fresh install) | Everything regenerates from scratch. Consistent, because there is no data expecting the old secrets |
| `rm` with snapshot failure | Abort; nothing deleted |
| `rm` on an app with `ext` volumes | Managed volumes deleted, ext contents untouched and reported |
| Rollback, app fully removed | Bare restore (§9.3) |
| Rollback, wrong master key | Refused in Phase A with the fingerprint spelled out |
| Rollback, archive fails checksum | Refused in Phase A; app untouched and still running |
| Archive contains a volume the restored manifest no longer declares | Not restored; **reported by name** |
| Restored manifest declares a volume the archive lacks | Created empty; **reported by name** |
| bsdtar instead of GNU tar | Fail at preflight with the `brew install gnu-tar` remedy |
| No TTY, sudo needs a password | Fail with instructions; never hang |
| Concurrent harbor command during a long snapshot | 5s lock timeout, and the message names the holder from the `harbor/lock` activity record |

---

## 12. What this deletes

- `harbor up`, `harbor down`.
- `run/<id>/source`, `HarborCtx._staged_sources()`, and the dual-source logic in `bundle_path()`.
- `RESERVED_APP_CONTENTS` — with the happ in its own subdirectory, name collisions with run-dir contents are structurally impossible.
- The snapshot staging dir and `state/*.json`: config, binds, and the manifest are already inside the run dir.
- `ContainerTarRunner`, `snapshot_image`, its `doctor` check, and the whole pre-pull problem (L10).
- The out-of-tree bundle machinery from the previous design: `_current_source`, `_literal`, `relinked_from`, the symlink-unlink branch in `_swap_bundle`, and the `--force-source`/`--keep-source` flags that preceded them.
- `AppDB`'s config and bind accessors, which move to the per-app store.

---

## 13. Deferred

`harbor update` is reserved. When it lands it should be: snapshot, stop,
re-stage, reconcile volumes, regenerate compose, start — with the volume rules
already decided here (orphaned → report, added → create empty, changed kind →
fail) and a **semantic** manifest diff (new required config, image bumps, route
changes) rather than a textual one, because that is what lets it refuse safely.
Route churn matters: a changed subdomain must be unregistered from the provider
*before* reallocation, or a stale proxy host is left pointing at a recycled port.

---

## 14. Relationship to `docs/snapshots.md`

That document describes the implementation on `jp/snapshots`. Carried forward:
D1 (ciphertext passthrough), D2 (`data` only), D4 (naming and manual pruning),
D5 (routes reallocated), D6 (`ext` bind restored, contents not), D7
(pre-rollback snapshot), D9 (trust compose's exit status), D10 (prior running
state; snapshot success survives a start failure), D11 (cold apps), D12 (wipe
`temp`), D13 (global lock).

Overturned: **D3** (bundle write target — now always the run dir), **D8/D14**
(root container and pinned image — now sudo), and the whole `index.json` shape
(now `snapshot.toml`, much smaller). N8 and N10 are superseded.

---

## 15. Code layout

| File | Responsibility |
| --- | --- |
| `harbor/lib/appconfig.py` | The per-app `config.logtab` store: params, secrets, binds, meta |
| `harbor/lib/lifecycle.py` | `stage`, `start`, `stop`, `rm` |
| `harbor/lib/snapshot/store.py` | `snapshots_root` layout, filenames, ref resolution, prune |
| `harbor/lib/snapshot/archive.py` | `snapshot.toml` model, the tar invocations, sha256, the privilege decision |
| `harbor/lib/snapshot/capture.py` | `harbor snapshot` |
| `harbor/lib/snapshot/restore.py` | `harbor rollback` |
| `harbor/cli/{stage,start,stop,rm,snapshot,rollback}.py` | Command modules |

Keep a `TarRunner`-style seam so tests can inject an unprivileged local
implementation; the sudo path is exercised by one test marked `docker`-style
opt-in. The test double **lives in `tests/`, not in `harbor/`** — a docstring
promising "test only" is not an invariant.

Style as elsewhere: ruff (88 cols, 2-space indent, double quotes), full type
hints, comments that say *why*. Tests must not reach the real docker daemon; the
autouse guard in `tests/conftest.py` enforces it.

---

## 16. Test plan

**Layout and stage**

1. `stage` copies the happ; `run/<id>/happ/manifest.toml` matches the catalog; no `source` symlink remains.
2. Editing `apps/<id>.happ` then re-staging re-copies.
3. Editing `run/<id>/happ/` then staging **loses** the edit — the run copy is not authoritative input.
4. `stage` preserves `config.logtab` and volume contents.
5. `stage PATH` creates the `apps/` symlink and stages from it; a conflicting existing entry is refused.
6. `app` volume link is relative and resolves inside `happ/`; managed links are absolute.
7. `app` volume mounts `:ro`; `readonly = false` on one is a manifest error.
8. Manifest drops a volume → link gone, data retained, reported. Adds one → created empty. Changes kind → refused.

**Config**

9. Config round-trips through `config.logtab`; `--set` before staging errors; `start --set` one-shot works.
10. Secret ciphertext is byte-identical after a snapshot/rollback cycle, and the plaintext never appears anywhere in the archive.
11. Re-staging does **not** regenerate an existing `auto` secret.

**Snapshot / restore**

12. Round trip: write files into a data volume → snapshot → clobber → rollback → contents restored, including dotfiles, nested dirs, and **an internal symlink that is still a symlink** (guards against `-h` and against `--transform` gaining the `s` flag).
13. Wipe-then-extract, not merge: a file created after the snapshot is gone after rollback.
14. `temp` wiped; `logs`/`bulk`/`ext` untouched; `ext` bind record restored with a warning.
15. Archive layout is exactly `snapshot.toml` + `run/<id>/…` + `volumes/data/<id>/…`.
16. Compressed and uncompressed archives are read by identical code paths.
17. Corrupt archive → refused in Phase A, app still running.
18. Wrong master key → refused with the fingerprint message.
19. Restore regenerates links and compose from the restored `happ/` — prove it by planting a bogus absolute symlink in the archive's run dir and asserting it is replaced.
20. Rollback never overwrites an existing `apps/<id>.happ`, but creates one when absent.

**rm**

21. `rm` snapshots, then removes run dir, managed volumes, and route entries; `apps/` entry and `snapshots/` survive.
22. `rm --no-snapshot` skips the snapshot.
23. Snapshot failure during `rm` aborts with **nothing deleted**.
24. `rm` then `rollback` restores the app from nothing (bare restore).
25. `rm` then `start` is a clean reinstall.

**Privilege**

26. Running as root does not invoke sudo.
27. No TTY and sudo unavailable → clear failure, no hang.
28. bsdtar detected at preflight with the remedy in the message.

---

## 17. Checklist

- [x] `run/<id>/{happ,config.logtab,volumes/<kind>}` layout; `source` symlink gone
- [x] `stage` / `start` / `stop` replace `up` / `down`; README, init output, and all e2e tests updated
- [x] `harbor/lib/appconfig.py`; `AppDB` config/bind accessors removed
- [x] `app` volumes forced read-only + manifest validation error
- [~] `harbor rm` unified: stop, run dir, managed volumes, routes, activity.
      The snapshot step and `--no-snapshot` wait on `harbor snapshot`; until
      then `rm` requires `-y` or a confirmation that says so.
- [ ] `snapshot.toml`; `index.json` and the staging dir gone
- [ ] sudo tar create/extract; `ContainerTarRunner`, `snapshot_image`, and the doctor image check removed
- [ ] GNU tar preflight check
- [ ] Restore regenerates links and compose; bare restore documented with the `master.key` caveat
- [x] `RESERVED_APP_CONTENTS` removed
- [x] §16 tests 1-9, 11, 21, 25 green; `ruff check` and `ruff format --check` clean
- [ ] `docs/snapshots.md` folded in and deleted when `jp/snapshots` rebases
