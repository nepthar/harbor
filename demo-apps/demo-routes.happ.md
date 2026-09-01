# Routes Demo

Three routes: `main` (bare `[app]` subdomain), `sub1` (`sub1-routes.<domain>`
when assigned), and `host_only` (host port until the operator assigns a
provider).

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Routes Demo"
description  = "Publishes primary, secondary, and host-only routes"
subdomain    = "routes"

[volumes]
# Ship the nginx config alongside the manifest and mount it read-only.
# kind = "app" resolves `src` relative to this happ.
app = { kind = "app", src = "app", readonly = true }

[run.main]
image  = "nginx:alpine"
volumes = { app = "/etc/nginx/templates" }

[run.main.routes]
# "main" is the bare [app] subdomain; non-private routes auto-assign to default_route_provider
main     = { port = "8081" }
# "sub1" will be "sub1-routes.<provider_domain>" when assigned
sub1     = { port = "8082" }
# private: available to publish, but not auto-assigned
host_only = { port = "8083", private = true }
```

One nginx server per route. nginx:alpine runs envsubst over `*.template`
files, filling in `${HAPP_DOMAIN}` (injected by harbor) before nginx starts.

```nginx happ_path="app/site.conf.template"
server {
    listen 8081;
    location / {
        default_type text/plain;
        return 200 'Hello from the server listening on the "main" route! This should be accessible by https://${HAPP_DOMAIN}\n';
    }
}

server {
    listen 8082;
    location / {
        default_type text/plain;
        return 200 'Hello from the secondary domain! This should be accessible at https://sub1-${HAPP_DOMAIN}\n';
    }
}

server {
    listen 8083;
    location / {
        default_type text/plain;
        return 200 'Hello from the route only accessible via host port\n';
    }
}
```
