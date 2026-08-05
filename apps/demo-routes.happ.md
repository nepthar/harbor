# Routes Demo

Three routes: `main` (published at the bare `[app]` subdomain), `sub1`
(published as `sub1-routes.<domain>`), and `lan_only` (plain docker port
forwarding, no subdomain).

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Routes Demo"
description  = "Publishes primary, secondary, and LAN-only routes"
subdomain    = "routes"

[volumes]
# Ship the nginx config alongside the manifest and mount it read-only.
# kind = "app" resolves `src` relative to this happ.
app = { kind = "app", src = "app", readonly = true }

[run.main]
image  = "nginx:alpine"
volumes = { app = "/etc/nginx/templates" }

[run.main.routes]
# "main" is always published under the raw, unprefixed subdomain as defined in [app]
main     = { port = "8081", publish = "web" }
# "sub1" will be published as "sub1-routes.<harbor_domain>".
sub1     = { port = "8082", publish = "web" }
# This will not receive a subdomain. "lan" is just regular docker port forwarding.
lan_only = { port = "8083", publish = "lan" }
```

One nginx server per route. nginx:alpine runs envsubst over `*.template`
files, filling in `${HAPP_DOMAIN}` (injected by harbor) before nginx starts.

```nginx happ_path="app/site.conf.template"
server {
    listen 8081;
    location / {
        default_type text/plain;
        return 200 'Hello from the server listening on the "main" route! This should be accessable by https://${HAPP_DOMAIN}\n';
    }
}

server {
    listen 8082;
    location / {
        default_type text/plain;
        return 200 'Hello from the secondary domain! This should be accessable at https://sub1-${HAPP_DOMAIN}\n';
    }
}

server {
    listen 8083;
    location / {
        default_type text/plain;
        return 200 'Hello from the route only accessable via lan\n';
    }
}
```
