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
5. **Container logs in the web UI, tailed live.** Nothing in the API or the
   UI exposes them; `/logs` is the Activity page, and container output belongs
   to dockerd. For a non-engineer whose app will not start, this matters more
   than harbor's own run logs. Two halves, and the second is the hard one: a
   backlog to open on (`docker compose logs --tail`, a normal request/response)
   and then a live tail (`docker compose logs -f`) that keeps writing while the
   page is open. The job modal already renders streamed output, so the front
   end is mostly there. What is new is a child process that outlives a job:
   `docker_run_command` reads to EOF and returns a `DockerReturn`, which a `-f`
   tail never reaches, so the tail needs its own spawn-and-stream path whose
   lifetime is the *reader's*, not a verb's. It has no exit status, it must be
   killed when the browser goes away rather than when a job finishes, and two
   viewers on one app should not mean two `docker` children. Per-unit
   selection, since an app can have several containers, and a byte cap so a
   chatty container cannot pin harbord's memory.
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
10. **A manifest check the daemon can perform.** The prerequisite for both
    editors below, and for showing an operator what a happ *does* before they
    install it. A verb taking manifest bytes and an app id, touching nothing
    on disk, returning either the `AppStack` it would build or every reason it
    could not. `AppStack.from_bytes(data, app_id, source)` is already exactly
    this call — `source` only names the file in messages, so a buffer that was
    never written can borrow the name it would have had. Three things stand
    between that and a useful answer:

    - *Errors are strings, and the caller needs structure.* Every failure
      arrives as one `ConfigError` whose message has been pre-formatted for a
      terminal — `_fmt_validation_error` flattens pydantic's `loc`/`msg`/
      `input` into indented lines, and `_validate_manifest` joins its list
      with `"\n  "`. A caller wants the parts back: which key, what is wrong,
      and for TOML syntax the line and column, so a failure can be marked
      where it happened instead of printed underneath. Keep the structured
      form and let the CLI's formatting be one renderer of it.
    - *Parsing is fail-fast, and the caller wants the whole list.* The stages
      are sequential — decode, TOML, schema, cross-section checks, then
      `_build` — and each raises on the first problem, so fixing one error
      only reveals the next. Pydantic already reports every schema violation
      at once and `_validate_manifest` already accumulates, so the collecting
      is half done; what is missing is running the later stages when an
      earlier one has found something survivable, and admitting that some
      stages genuinely cannot (nothing downstream of unparseable TOML can
      say anything useful). The honest contract is a list of errors plus
      whether parsing got far enough for that list to be complete.
    - *Not every objection is an error.* A `ComposeWarning` (see "Privileged
      compose keys" below) is not a manifest that failed to parse — it is a
      manifest that parsed and is asking for something unmodelled. The check
      returns those separately from errors, so a UI can render "this will not
      load" and "review these options" differently. The catalog view already
      serialises them; the check would hand back the same shape for text that
      has not been saved yet.

    A serialised `AppStack` on success is worth more than a bare "ok": it is
    what a UI needs to show what a manifest *does* — the units, volumes,
    routes, and config keys it produces, and the free-form docker options it
    passes through — before anything is installed or written.

11. **Editing a happ from the web UI — the bundle, not the staged copy.**
    Editing `run/<app>/happ/` is editing derived state: `install` regenerates
    it from the bundle and `reset` re-stages, so edits vanish silently, and
    `manifest_stale` only watches drift in the other direction. Edit
    `apps/<app>.happ` instead, framed as "customise this app", after which
    `manifest_stale` lights up and Reinstall applies it. Item 10 is what makes
    this an editor rather than a text box: without a dry check, the only way
    to find out whether an edit is good is to write it. Note this also
    punches through the rule in the `JOBS` comment — a manifest defines bind
    mounts, which means root — so it belongs behind authentication and behind
    the capability receipt. Low value for the target audience, who do not
    write manifests; real value for the tinkering loop in the case study.

12. **Editing config.toml from the web UI, in the same style.** Same shape as
    item 11, one file up: an editor, a dry check, and an explicit write. The
    write half is the part that already exists and is worth not rebuilding —
    `config_edit._commit` stages the new text beside the real file, runs
    `load_config_file` over the staging copy, and only then `os.replace`s it
    into place, so a config that does not load can never land. Splitting the
    check out from the commit gives the editor its dry run for free. Three
    things this file has that a manifest does not:

    - *`[repo]` is soft-failed on purpose.* `_resolve_repos` logs and drops
      every repo rather than raising, so that one typo cannot take down every
      harbor command. That is right for startup and wrong for an editor: a
      dry check that calls a broken `[repo]` table valid is lying, so the
      check has to report the soft failures alongside the hard ones and mark
      which is which.
    - *Comments are the operator's.* Edits made through `edit_config` go
      through tomlkit and round-trip comments, ordering, and whitespace.
      Whole-file editing keeps that for free — the operator's text *is* the
      document — but it means the UI must never round-trip through a parsed
      structure and re-serialise.
    - *Nothing takes effect by itself.* `_ctx_again` already exists for
      re-reading config after an edit, and route assignments deliberately
      land on the next `start`. So the UI has to say which parts of a saved
      change are live and which wait for a restart, rather than implying the
      whole file took hold.

    Also worth stating plainly in the UI: this is the file that defines host
    volumes and route providers, so it is root-adjacent in the same way item
    11 is, and belongs behind the same authentication.

### Deliberately not doing

**Built-in metric collection and dashboarding.** It is a whole product
category, and building it means owning a time-series store, retention, and
disk-full behaviour — against "smallest thing that works" and against
no-lock-in. The target user wants "is it running" and "am I out of disk",
which item 4 covers; the user who wants graphs is an engineer who will run
their own. Be an excellent host for it instead: put Netdata, or
Prometheus and Grafana, in the default repo as one-click happs.

## Privileged compose keys — now visible, not yet granted

*Mostly closed.* `[run.<unit>.compose]` is still copied verbatim into the
generated compose service, and `_COMPOSE_MANAGED_KEYS` in `lib/manifest.py`
still stops a manifest from *shadowing* what harbor generates (`image`,
`volumes`, `ports`, `network_mode`, …). What changed is that everything else
no longer passes through *silently*.

`_COMPOSE_ALLOWED_KEYS` is an allowlist of keys that shape how a container runs
without reaching outside it — `healthcheck`, `depends_on`, `mem_limit`, `user`,
`ulimits`, `read_only`, and friends. An allowlist rather than a denylist, so a
compose key nobody has considered yet is remarked on instead of sailing
through. Anything not on it becomes a `ComposeWarning` on the `AppStack`, one
per run unit, derived from `run_units` so it cannot drift from what compose is
actually handed.

The warning does not try to classify what the key does. Harbor cannot know
that, and pretending to would mean maintaining a table of every compose key
against every docker version. It says what it does know:

> This application sets free-form docker options on `<run_unit>` that are not
> guaranteed to be safe. Please review them before continuing

…followed by the offending `key = value` pairs, so the operator reviews the
actual request rather than harbor's summary of it. This also catches the
typo case for free: compose *ignores* a key it does not know, so `devcies`
for `devices` silently does nothing, and now shows up beside the real ones.

Three places surface it, all before the operator commits: `danger_callouts`,
so `harbor inspect` and `harbor start` list it beside host networking and
writable binds; `harbor install`, which prints it and asks, unless `-y` is
passed; and the web UI's Repos page, which prints it on the app card beside the
manifest that asks for it, above the Install button.

**What is deliberately still missing is the grant.** A warning is a warning:
nothing refuses to install or start, and there is no per-app record that an
operator once said yes. The shape that would close it is the one `kind = "host"`
volumes already use — an ungranted key becomes a `ConfigIssue`, which makes it
a `start_blocker`, and the operator grants once per key, recorded in the app
store beside binds and route assignments (`harbor config <app> --allow
privileged`). That is a bigger change than it looks: it moves an
installation-independent property of a manifest onto per-installation state,
and `AppStack` is deliberately installation-independent today.

Deliberately **not** a prompt inside `manifest.py`: it has no `Conn` to ask
from, jobs submitted through harbord cannot answer one, and a question asked on
every `start` trains the operator to dismiss it. The `install` prompt sits at
the CLI layer for exactly that reason.

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
