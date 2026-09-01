# Hello World

The smallest harbor app: one container, one command, no state.

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Hello world"
description  = "Says hello!"
source       = "github:nepthar/harbor/main/demo-apps/hello-world.happ.md"

[run.main]
image  = "alpine:latest"
cmd    = ["/bin/sh", "-c", "echo \"hello world!\"; echo; env; echo"]
restart = "no"
```
