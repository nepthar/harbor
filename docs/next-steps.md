# Next steps

Known gaps, written down so they stop being remembered. Nothing below
"Ordered work" is scheduled, and nothing here is a spec — each entry says what
exists today and, where a shape was already agreed, what it should become.

## Who this is for

The audience harbor is trying to reach is someone technical enough to install
Linux on an old machine and paste terminal commands, but not a software
engineer. "More people can run homelab stuff."

That names the competition, and it is not Kubernetes tooling. Helm is a
rounding error among self-hosters — Docker Compose is the default, Kubernetes
is a vocal minority skewed toward people who use it professionally, and
TrueNAS SCALE moved its apps to Kubernetes and then back to Compose. The real
comparison set is **Unraid, CasaOS, Umbrel, YunoHost, Runtipi**, and every one
of them leads with an app store and a first run that ends in a browser tab.

Against Helm, harbor competes on simplicity and wins. Against CasaOS it
competes on catalog size and first-run polish, and today it would lose. So
work that grows the catalog or shortens the first fifteen minutes outranks
work that improves the architecture, even though the architecture is the part
that is genuinely better.

Two assets to lean on, both underused. `.happ.md` is one committable file
containing a manifest and its scripts, which is dramatically easier to publish
than a Helm chart or a compose-plus-README — that is the growth loop. And the
no-lock-in property is a *trust* argument that fits in one non-technical
sentence: if you outgrow harbor, or it stops being maintained, your apps keep
running. CasaOS and Umbrel users have been burned exactly there.

## Ordered work

Roughly in the order that unblocks the most. The first two gate everything
user-facing below them.

1. **A systemd unit and a one-command installer.** `harbord` is foreground-only
   and dies with its shell, so the target user installs, pastes, closes the
   laptop, and everything stops. The installer should end by printing the URL.
2. **Authentication on the admin API.** There is none. Fine behind an ssh
   tunnel; not fine on a LAN with a smart TV on it, and the UI is the product
   for this audience. `HarborStore.set_token` already has expiring tokens.
3. **App sources become repos.** Today `app_sources` is `dict[str, Path]` —
   local directories — and `fetch` handles one app at a time. A repo is a link
   to a folder of happs on a public GitHub page, addable from the web UI. This
   is the single highest-value item: it turns a 14-app catalog into something
   that grows without the maintainer, and it is where a curated official repo,
   enabled by default, would live. Three things to get right: trust is
   per-repo rather than per-app, because adding one is a standing commitment
   to whatever appears there later, so say so in the UI and keep the per-app
   capability receipt at install; ship the default repo so day one is not an
   empty store; and surface which repo an app came from, since two repos can
   carry one id (`ambiguity_message` and `doctor` already handle the
   collision).
4. **A dashboard.** There is none — the page says "Hello. Connected to
   harbor". Host CPU, memory, filesystem, uptime, apps running, updates
   available. All cheap; take disk from `statvfs` rather than the volume
   walker. Keep per-app resource usage out of it: that needs `docker stats`,
   which is slow, and it is the first step toward owning a metrics product.
5. **Container logs in the web UI.** Nothing in the API or the UI exposes
   them; `/logs` is the Activity page, and container output belongs to
   dockerd. For a non-engineer whose app will not start, this matters more
   than harbor's own run logs. The job modal is the obvious place to render
   them.
6. **Health, not just liveness.** `HarborRunUnitStatus` keeps `state` and
   discards docker's `Status`, which carries `(healthy)` / `(unhealthy)`. A
   container can be running and the app dead — precisely the failure this
   audience cannot diagnose. Surfacing the field is nearly free; a `[health]`
   probe in the manifest would go further.
7. **Somewhere for backups to go.** `snapshot` / `restore` is a real
   differentiator: Helm has no equivalent and neither do most app-store
   OSes. But snapshots land on the disk that dies. A destination — USB,
   another box, S3 — turns a feature into a reason to trust harbor with
   family photos.
8. **A first-run wizard.** After the installer, walk through `harbor_address`,
   a route provider, and a first app, rather than opening on an empty
   catalog.
9. **Batteries-included HTTPS.** The route-provider abstraction is good, but
   both providers presume Pangolin or NPM already exists, which presumes
   someone who understands reverse proxies. A bundled Caddy provider giving
   LAN-local HTTPS with no configuration would finish the abstraction.
10. **Editing a happ from the web UI — the bundle, not the staged copy.**
    Editing `run/<app>/happ/` is editing derived state: `install` regenerates
    it from the bundle and `reset` re-stages, so edits vanish silently, and
    `manifest_stale` only watches drift in the other direction. Edit
    `apps/<app>.happ` instead, framed as "customise this app", after which
    `manifest_stale` lights up and Reinstall applies it. Note this also
    punches through the rule in the `JOBS` comment — a manifest defines bind
    mounts, which means root — so it belongs behind authentication and behind
    the capability receipt. Low value for the target audience, who do not
    write manifests; real value for the tinkering loop in the case study.

### Deliberately not doing

**Built-in metric collection and dashboarding.** It is a whole product
category, and building it means owning a time-series store, retention, and
disk-full behaviour — against "smallest thing that works" and against
no-lock-in. The target user wants "is it running" and "am I out of disk",
which item 4 covers; the user who wants graphs is an engineer who will run
their own. Be an excellent host for it instead: put Netdata, or
Prometheus and Grafana, in the default repo as one-click happs.

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

- **The web UI has no authentication** (see item 2 above). Anything that can open `admin.sock`
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
- **harbor-ui has no tests.** Catalog update-check is covered on the daemon
  (`POST /catalog/check`); the happ itself is HTML builders and FastAPI
  routes with no TestClient coverage.
- **Volume sizes are computed per request.** `views.app_view` walks every file
  under a volume, so the app detail page pays for it on every load and the list
  view omits sizes entirely to stay cheap.
- **Rootless docker breaks snapshot and restore silently.** `lib/lifecycle/
  rootfs.py` assumes the container's root can read root-owned volume files;
  under userns that maps back to the invoking user. Nothing checks.
- **`fetch` is on the admin API without a source allowlist.** It takes a
  github: target and nothing else -- `parse_target` refuses any other shape --
  and it only copies files into the apps root, so nothing it fetches runs
  until someone stages and starts it separately. What is still missing is a
  restriction on *which* repositories: today any public one will do.
