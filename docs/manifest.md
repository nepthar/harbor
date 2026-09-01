# The manifest

Every happ is a `manifest.toml` plus, optionally, the files it ships with. The
manifest is the whole of what harbor knows about an app: what containers to
run, what storage they need, what the operator has to fill in, and what the
outside world can reach. Everything else — the compose file, the run
directory, the volume links — is generated from it.

This is the reference. For a manifest built from nothing in one sitting, read
the [case study](case_study.md).

A manifest is TOML, and unknown sections and keys are refused rather than
ignored, so a typo is an error at install time instead of a setting that
silently did nothing.

---

## `[app]`

The only required section, and the only one that accepts keys harbor does not
know: extra keys are kept and shown verbatim on the app's page, so an author
can carry `author`, `source`, `license` and the like.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `version` | string | **required** | The happ's version. Yours, not the image's. |
| `app_id` | string | from the filename | Refused if it disagrees with the filename. |
| `display_name` | string | `""` | Shown in the UI and `harbor ps` instead of the id. |
| `description` | string | `""` | One line. Shown in the catalog. |
| `main` | identifier | `"main"` | Which `[run]` unit is the app itself. Must exist. |
| `network_mode` | `normal` \| `host` | `normal` | `host` drops port isolation and is called out as dangerous. |
| `subdomain` | identifier | none | DNS label routes are published under. Becomes a config key the operator can override. |

```toml
[app]
version      = "1.4.0"
display_name = "Mealie"
description  = "Manage, save, share recipes and make shopping lists"
subdomain    = "recipes"
```

## `[run.<unit>]`

One container each. A single-container app declares just `[run.main]`; units
reach each other by unit name as hostname, on a private network harbor
creates.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `image` | string | **required** | Pin a tag. `latest` makes a happ unreproducible. |
| `cmd` | list of strings | image default | Overrides the image's command. |
| `volumes` | `{ <volume> = "<path in container>" }` | `{}` | Every name must be declared in `[volumes]`. |
| `env` | `{ KEY = "value" }` | `{}` | `${…}` placeholders are substituted; see below. |
| `routes` | table of `[run.<unit>.routes.<name>]` | `{}` | Ports the outside world may reach. |
| `restart` | `no` \| `always` \| `on-failure` \| `unless-stopped` | `unless-stopped` | Compose restart policy. |
| `compose` | table | `{}` | The escape hatch. See [Free-form docker options](#free-form-docker-options). |

```toml
[run.main]
image   = "lscr.io/linuxserver/unifi-network-application:10.4.57"
volumes = { app_config = "/config" }
env     = { MONGO_HOST = "unifi-db", MONGO_PASS = "${mongo_pass}" }

[run.unifi-db]
image   = "docker.io/mongo:8.0.11"
volumes = { db_data = "/data/db" }
```

## `[volumes]`

What the app needs to keep, and what kind of thing it is. The operator decides
*where* each kind lives, once, in `config.toml` — the manifest only says which
kind it wants. That split is the point: a happ that says `kind = "bulk"` lands
on the big disk on a machine that has one and in the default root on a machine
that does not, with no change to the happ.

| Kind | For |
| --- | --- |
| `data` | State the app must not lose. What gets snapshotted. |
| `bulk` | Large data — media libraries, archives. Usually a separate disk. |
| `logs` | Output that can be rotated away without loss. |
| `temp` | Caches and scratch. Safe to delete when the app is not running. |
| `app` | Files the happ itself ships. Always mounted read-only. |
| `host` | A directory on the machine, chosen by the operator at install time. |

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `kind` | one of the above | **required** | |
| `desc` | string | `""` | Shown to the operator, and worth writing for `host` volumes. |
| `readonly` | bool | `false` | `app` volumes are always read-only; setting `readonly = false` on one is an error. |
| `src` | string | volume name | `app` volumes only: which file or directory in the bundle to mount. |

```toml
[volumes]
db_data     = { kind = "data", desc = "the recipe database" }
media       = { kind = "host", desc = "where your photo library already lives" }
init_script = { kind = "app", src = "init-mongo.sh" }
```

A `host` volume is not a path — it is a *request*. Harbor will not start the
app until the operator binds it to one of the host volumes they declared:

```
harbor config <app> --bind media=photos
```

## `[config]` and `[adv_config]`

Values the operator supplies. `[adv_config]` is the same thing, marked as
noise a normal operator should not have to read. The two share one namespace,
so a name may appear in only one of them.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `desc` | string | `""` | What this is, in the operator's words rather than the app's. |
| `default` | string | none | With no default, the app will not start until the value is set. |
| `secret` | bool | `false` | Stored encrypted, never returned by the API or shown in the UI. |

`default = "auto"` on a secret means harbor generates one at install and the
operator never sees or sets it — the right answer for a password two
containers need to agree on and nobody else needs.

```toml
[config]
admin_email = { desc = "Login for the web interface" }
timezone    = { desc = "IANA timezone", default = "UTC" }

[adv_config]
mongo_pass  = { secret = true, default = "auto" }
```

Set them with `harbor config <app> --set timezone=America/Denver`, or from the
app's page in the web UI.

## `[run.<unit>.routes.<name>]`

A named port the outside world may reach. The route named `main` is published
at the app's bare subdomain; every other name gets `<name>-<subdomain>`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `port` | string | **required** | `"8080"` (harbor picks the host port), `"8443:8443"` (pinned), or either with `/udp`. |
| `scheme` | `http` \| `https` | `http` | How a reverse proxy should *dial the container*, not what a browser sees. |
| `private` | bool | `false` | LAN-only: never handed to a route provider. |
| `desc` | string | `""` | Shown beside the URL. |

```toml
[run.main.routes]
main    = { port = "8443", scheme = "https" }
metrics = { port = "9090", private = true, desc = "Prometheus scrape target" }
```

Pin a host port only when something outside harbor already points at it — a TV
that expects `:8096`, or an app that advertises its own port. Otherwise let
harbor allocate, and collisions stop being your problem.

## `[commands.<name>]`

Operations the app declares for itself, runnable from the CLI or as a button
in the web UI. This is how a happ ships its own maintenance: a backup, a
reindex, a password reset.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cmd` | string or list | **required** | A string runs through `/bin/sh -c` and takes operator arguments; a list is argv. |
| `run_unit` | identifier | `"main"` | Which container to run it in. Must exist in `[run]`. |
| `desc` | string | `""` | Shown in `harbor cmd <app>` and in the UI. |

```toml
[commands.backup]
cmd      = "mealie-cli backup create"
desc     = "Write a backup into the data volume"

[commands.psql]
cmd      = ["psql", "-U", "postgres"]
run_unit = "database"
desc     = "Open a database shell"
```

## Substitution in `env`

`${…}` in `[run.<unit>.env]` is resolved against one flat keyspace, at install
time, and a reference to something that does not exist is an error rather than
an empty string:

- `${<config key>}` — anything from `[config]` or `[adv_config]`.
- `${routes.<name>}` — the full public URL of a declared route.
- `${happ.domain}`, `${happ.volumes}`, `${happ.cmd}`, `${happ.routes}` — the
  app's own resolved values.

Every unit also gets `HAPP_ID`, `HAPP_VERSION`, and `HAPP_RUN_UNIT` for free.

```toml
[run.main.env]
# Jellyfin advertises this address to clients on the network.
JELLYFIN_PublishedServerUrl = "${routes.main}"
DB_PASSWORD                 = "${mongo_pass}"
```

## Free-form docker options

`[run.<unit>.compose]` is copied verbatim into that unit's compose service, for
the things harbor does not model:

```toml
[run.main.compose]
mem_limit = "256m"

[run.main.compose.healthcheck]
test     = "redis-cli ping || exit 1"
interval = "10s"
```

Two rules apply.

**Keys harbor generates are refused** — `image`, `volumes`, `ports`, `labels`,
`environment`, `command`, `hostname`, `restart`, `network_mode`. Those have
manifest fields; setting them twice would mean one of them silently losing.

**Keys harbor does not recognise are announced.** There is an allowlist of
options that shape how a container runs without reaching outside it —
`healthcheck`, `depends_on`, `mem_limit`, `user`, `ulimits`, `read_only` and
friends — and anything outside it produces a warning on install, in `harbor
inspect`, and on the app's card in the web UI:

> Warning: This application sets free-form docker options on main that are not
> guaranteed to be safe. Please review them before continuing

Nothing is refused: the machine belongs to the operator, and `privileged =
true` is a legitimate thing for a happ to need. But harbor cannot know what an
arbitrary compose key does, so it says so and shows the operator exactly what
was asked for. This also catches typos — compose silently ignores a key it
does not know, so a misspelled `devcies` would otherwise do nothing at all
and say nothing about it. See [demo-danger](../demo-apps/demo-danger.happ.md).

## A happ in one file

Anything above can live in a `<app_id>.happ.md` markdown file instead of a
folder, with the manifest and any scripts in fenced code blocks tagged with
their path:

````markdown
# My App

Whatever prose you like, including why the manifest looks the way it does.

```toml happ_path="manifest.toml"
[app]
version = "1.0.0"
```

```sh happ_path="start.sh:+x"
#!/bin/sh
exec my-app --config /config
```
````

The `:+x` suffix marks the extracted file executable, which a script you
intend to run as a container command needs.

One file, committable anywhere, readable as documentation by someone who has
never run harbor. See [demo-markdown](../demo-apps/demo-markdown.happ.md).

## Checking your work

```bash
harbor inspect <app>      # what the manifest declares, resolved
harbor install <app>      # parse errors, one list, with the key that caused each
```

`harbor inspect` on an uninstalled app reads the manifest it *would* install
from, so it is the fastest way to see whether an edit did what you meant.
