# Volume Showcase

One volume of each kind: `app` (files shipped with the bundle, read-only),
`data` (persisted), `temp` (scratch), and `host` (a tagged host directory you
must bind before starting). The script just lists what got mounted.

```toml klso_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Volume Showcase"
description  = "Demonstrates volumes and binding of a host directory"

[volumes]
app   = { kind = "app", desc = "The contents of the ./app folder" }
state = { kind = "data", desc = "A data-type volume called state to store the state of this app" }
temp  = { kind = "temp", desc = "scratch space - no guarantee it will be persisted run to run"}
files = { kind = "host", desc = "Host directory to inspect, must be set before running", readonly = true }

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "/kelso/app/list_volumes.sh"]
volumes = { app = "/kelso/app", state = "/kelso/state", files = "/kelso/host_files", temp = "/kelso/tmp" }
restart = "no"
```

The script, extracted to `app/list_volumes.sh` and marked executable:

```bash klso_path="app/list_volumes.sh:+x"
#!/bin/sh
# volumes are mounted via KLSO_VOLUMES ("name:/guest/path,name:/guest/path").

date

echo "Hello, here are the volumes I was passed (from \$KLSO_VOLUMES)"

echo "$KLSO_VOLUMES" | tr ',' '\n' | while IFS=':' read -r name path; do
  echo
  echo "Volume '$name' mounted at $path:"
  ls -al "$path"
done
```
