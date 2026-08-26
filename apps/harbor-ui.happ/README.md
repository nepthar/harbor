# Harbor UI

The harbor web interface. Reads harbor state over the admin socket that
`harbord` publishes and renders it server-side — no polling, no client
framework, no build step. The `ui/` directory is the application. `start.sh`
runs `uv sync` into a `temp` volume at `/venv`, then serves with uvicorn.
The venv survives container recreate; uv rebuilds it if the image Python
no longer matches.

| File | What it is |
|---|---|
| `start.sh` | Container entrypoint: `uv sync`, then uvicorn |
| `server.py` | FastAPI app: routes, GET/POST handlers |
| `pyproject.toml` | fastapi, uvicorn, python-multipart |
| `uv.lock` | Frozen resolve for `uv sync --frozen` |
| `api.py` | Client for harbord (unix socket or TCP) |
| `layout.py` | Page chrome: CSS, JS, nav, shared fragments |
| `catalog.py` | Catalog listing, app cards, fetch, updates |
| `installed.py` | Installed-apps list and the app detail page |
| `volumes.py` | Host volumes and harbor-managed storage |
| `activity.py` | Unattended-run history and per-run output |

## Setup

It needs `harbord` running on the host, and `$harbor/var/conn` bound in. For now
that is manual — declare the directory as a host volume:

```
harbor config-sys host-volume --add harbor_conn=${harbor_root}/var/conn
harbor config harbor-ui --bind conn=harbor_conn
harbor start harbor-ui
```

On a host whose bind mounts cannot carry a unix socket — Docker Desktop on
macOS, where the socket is visible in the container and unusable — run
`harbord --port N --host 0.0.0.0` and point the app at it over TCP instead:

```
harbor config harbor-ui --set api_address=host.docker.internal:N
```

## Trust

Anything that can open the admin socket can run every verb the API exposes, so
this app is exactly as privileged as `harbord` is. It is the one happ that
should be bound to it, and it has no authentication of its own — keep it on a
trusted network until that changes.
