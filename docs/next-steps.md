# Next steps

Known gaps, written down so they stop being remembered. Nothing here is
scheduled, and nothing here is a spec — each entry says what exists today and,
where a shape was already agreed, what it should become.

## Privileged compose keys are invisible

`[run.<unit>.compose]` is copied verbatim into the generated compose service.
The only guard is `_COMPOSE_MANAGED_KEYS` in `lib/manifest.py`, which stops a
manifest from *shadowing* what harbor generates (`image`, `volumes`, `ports`,
`network_mode`, …). Everything else passes through untouched.

So this manifest is accepted, generates what it asks for, and is reported as
clean:

```toml
[run.main.compose]
privileged = true
pid        = "host"
cap_add    = ["SYS_ADMIN"]
devices    = ["/dev/sda:/dev/sda"]
```

```
manifest accepted: True
compose.privileged = True
compose.pid = host
compose.cap_add = ['SYS_ADMIN']
compose.devices = ['/dev/sda:/dev/sda']
danger_callouts: NOTHING FLAGGED
```

`privileged = true` is host root: the container can mount the host disk. No
bind, no config change, no prompt — `harbor fetch` then `harbor start`.

This is not a claim that harbor should sandbox hostile apps; it should not,
and the box is administered by the person running it. The problem is narrower
and worse: `danger_callouts` exists precisely to make escalation legible, and
it only knows about `network_mode = "host"` and writable host binds. An
operator auditing with `harbor inspect` rather than by reading raw TOML gets a
clean bill of health on the manifest above.

**The shape it should take** is the one `kind = "host"` volumes already use —
the manifest states what it needs, and the operator provides it:

- An **allowlist** of benign compose keys (`healthcheck`, `deploy`, `user`,
  `read_only`, `ulimits`, `stop_grace_period`, …) that pass silently. Allowlist
  rather than denylist, so a compose key nobody has considered yet needs a
  grant instead of sailing through.
- Anything else is a *declared request*, not a parse error.
- An ungranted request becomes a `ConfigIssue`, so it is a `start_blocker`.
  `start` already refuses on those and prints `recovery_lines`, so the refusal
  and its wording come free.
- The operator grants once, per key, recorded in the app store beside binds and
  route assignments: `harbor config <app> --allow privileged`.
- `danger_callouts` lists requested and granted separately, so `harbor inspect`
  shows the escalation instead of hiding it.

Deliberately **not** a prompt. `manifest.py` has no `Conn` to ask from, jobs
submitted through harbord cannot answer one, and a question asked on every
`start` trains the operator to dismiss it.

## Smaller known gaps

- **The removal verbs are CLI-only.** `uninstall`, `reset`, and `rm` are not
  in the daemon's job registry, so the web UI cannot remove anything. That is
  deliberate while the API has no authentication -- see below -- but it means
  an operator who only uses the UI has no way to uninstall.

- **No systemd unit.** `harbord` is foreground-only and dies with its shell.
- **The web UI has no authentication.** Anything that can open `admin.sock`
  runs every verb the API exposes. Fine over an ssh tunnel; not fine on a
  public route. `HarborStore.set_token` already has expiring tokens. This got
  sharper when `fetch` joined the API: the verb list is no longer only ids of
  things that already exist, so an unauthenticated caller can now pull code
  from GitHub into the apps root. It still cannot stage or start it. FastAPI
  also left `/docs`, `/redoc`, and `/openapi.json` on that same surface.
- **A `cmd` job holds the app lock for the command's whole run.** Harbor-wide
  ops can proceed; the same app cannot be staged, started, or stopped until it
  exits. Fine for the batch-style commands the UI is for; a long-runner still
  wedges that app. The runner also allocates no TTY, so a command that waits
  on stdin hangs rather than prompting.
- **Only daemon jobs file activity output.** A CLI invocation prints to the
  operator's terminal and records only its status line in `activity.logtab`,
  so the UI's Activity page shows what harbord ran, not what the operator
  typed. The mechanism to close this is in place — `Job.call(args, ctx,
  echo=stream)` writes the run log and the terminal from one stream — and the
  plan is to migrate CLI verbs onto their Job classes, verb by verb.
- **The Activity page shows harbor runs, not container logs.** `harbor logs`
  streams `docker compose logs`; the UI has no equivalent yet.
- **harbor-ui has no tests.** Catalog update-check is covered on the daemon
  (`POST /catalog/check`); the happ itself is HTML builders and FastAPI
  routes with no TestClient coverage.
- **Volume sizes are computed per request.** `views.app_view` walks every file
  under a volume, so the app detail page pays for it on every load and the list
  view omits sizes entirely to stay cheap.
- **Rootless docker breaks snapshot and restore silently.** `lib/lifecycle/
  rootfs.py` assumes the container's root can read root-owned volume files;
  under userns that maps back to the invoking user. Nothing checks.
- **`harbor inspect` still requires staging for an app id**, unlike `config`,
  which now falls back to the bundle in an app source.
- **`fetch` is on the admin API without a source allowlist.** It takes a
  github: target and nothing else -- `parse_target` refuses any other shape --
  and it only copies files into the apps root, so nothing it fetches runs
  until someone stages and starts it separately. What is still missing is a
  restriction on *which* repositories: today any public one will do.
- **Job output misses log lines.** `JobRunner` records a verb's return value;
  progress written through `logging` (including the container steps in
  `rootfs.py`) goes to harbord's stderr and never reaches `job.output`.
