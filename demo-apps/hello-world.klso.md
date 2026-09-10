# Hello World

The smallest kelso app: one container, one command, no state.

```toml klso_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Hello world"
description  = "Says hello!"
source       = "github:nepthar/kelso/main/demo-apps/hello-world.klso.md"

[run.main]
image  = "alpine:latest"
cmd    = ["/bin/sh", "-c", "echo \"hello world!\"; echo; env; echo"]
restart = "no"
```
