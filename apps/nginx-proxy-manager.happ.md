# Nginx Proxy Manager

A real app, not a demo: reverse proxy with a web UI and letsencrypt certs.
Runs with `network_mode = "host"` so it can bind ports 80/443 directly; two
data volumes hold its config and certs.

```toml happ_path="manifest.toml"
[app]
version      = "1.0.0"
display_name = "Nginx Proxy Manager"
description  = "Reverse proxy, w/ ssl certs managed by letsencrypt. Pinned to :latest"
source       = "github:nepthar/harbor/main/apps/nginx-proxy-manager.happ.md"
network_mode = "host"

[volumes]
data    = { kind = "data" }
letsenc = { kind = "data" }

[run.main]
image   = "jc21/nginx-proxy-manager:latest"
volumes = { data = "/data", letsenc = "/etc/letsencrypt" }
```
