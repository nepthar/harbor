# Kelso UI

The kelso web interface. Reads kelso state over the admin socket that
`kelsod` publishes and renders it server-side — no polling, no client
framework, no build step. The `ui/` directory is the application. `start.sh`
runs `uv sync` into a `temp` volume at `/venv`, writes a self-signed
certificate if there isn't one, then serves https with uvicorn. The venv
survives container recreate; uv rebuilds it if the image Python no longer
matches.

| File | What it is |
|---|---|
| `start.sh` | Container entrypoint: `uv sync`, certificate, then uvicorn |
| `server.py` | FastAPI app: routes, GET/POST handlers |
| `pyproject.toml` | fastapi, uvicorn, python-multipart, cryptography |
| `uv.lock` | Frozen resolve for `uv sync --frozen` |
| `api.py` | Client for kelsod (unix socket or TCP) |
| `auth.py` | The password, the signed session cookie |
| `tls.py` | The self-signed certificate, written once |
| `layout.py` | Page chrome: CSS, JS, nav, shared fragments |
| `dashboard.py` | Homepage: host CPU and memory charts |
| `static/` | Vendored browser assets, served at `/static` |
| `catalog.py` | Catalog listing, app cards, fetch, updates |
| `installed.py` | Installed-apps list and the app detail page |
| `volumes.py` | Host volumes and kelso-managed storage |
| `activity.py` | Unattended-run history and per-run output |

## Setup

It needs `kelsod` running on the host, and `$kelso/var/conn` bound in. For now
that is manual — declare the directory as a host volume:

```
kelso config-sys host-volume --add kelso_conn=${kelso_root}/var/conn
kelso config kelso-ui --bind conn=kelso_conn
kelso start kelso-ui
```

On a host whose bind mounts cannot carry a unix socket — Docker Desktop on
macOS, where the socket is visible in the container and unusable — run
`kelsod --port N --host 0.0.0.0` and point the app at it over TCP instead:

```
kelso config kelso-ui --set api_address=host.docker.internal:N
```

## Signing in

One password, no accounts. It is the `admin_pass` config value, so it is set
and reset the way every other kelso secret is:

```
kelso config kelso-ui --set admin_pass=<password>
kelso reload kelso-ui
```

The app will not start until it has one. Signing in mints a cookie signed
with a key derived from the password and good for seven days, which means
resetting the password signs every browser out — and is the only way to,
since nothing about a session is stored server-side. There is nowhere to
change the password from inside the UI, on purpose: the shell you administer
kelso from is the one place that can.

## Rate limiting

Two fixed-window buckets. A window opens on the first request after an idle
gap; what fits in it, fits, and the rest of that window is refused.

| Bucket | Allowed | Window | Applies to |
|---|---|---|---|
| general | 10 | 1 second | everything |
| auth | 10 | 1 hour | `POST /login`, on top of the general one |

Ten a second absorbs a burst — a cold page load is four requests in the same
millisecond — while capping the sustained rate. Ten an hour is 240 sign-in
attempts a day, which is not a dictionary attack. A sign-in spends from both,
so a flood exhausts itself against the per-second budget before it can eat far
into the hour's guesses.

`/static` is outside both: a refused stylesheet renders a page that looks
broken rather than one that says it is limited.

One budget for the whole app, not one per client: behind a reverse proxy every
request arrives from the proxy's address, so per-client counting would be
counting one client. That means anyone who can reach this app can bring it to
a halt, and anyone who can reach the sign-in form can spend the hour's
attempts — the accepted trade for a LAN interface. Sessions already issued are
unaffected, and `kelso reload kelso-ui` clears both counts. A refusal does
not move the window, so service resumes when the flooding stops rather than
staying shut.

Over the limit, a browser gets a page saying so and the job modal's `fetch`
gets the same sentence as JSON, both with an honest `Retry-After`. Which one
you get is decided by the `Accept` header, not the method — the sign-in form
is a POST too.

## Trust

Anything that can open the admin socket can run every verb the API exposes, so
this app is exactly as privileged as `kelsod` is. It is the one bundle that
should be bound to it, which is why it serves TLS itself rather than trusting
whatever is in front of it — the password is never on the wire in the clear,
even between a reverse proxy and this container.

The certificate is self-signed and written into the `tls` data volume on first
start, so a browser warns once and the fingerprint holds across restarts. The
route declares `scheme = "https"`, which is what tells a proxy to dial the
container over TLS; nginx-proxy-manager accepts a self-signed upstream as it
is, and a Traefik-backed provider needs its `serversTransport` set to skip
verification. Removing the volume regenerates the pair on the next start.
