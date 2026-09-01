# Config Demo

Shows `[config]`: a required value you must set, and a secret with an
auto-generated default. Config values reach the container through `${...}`
substitution in env vars.

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Hello, configuration!"
description  = "Says whatever you configure it to say!"

[config]
message = { desc = "The message to announce. This must be configured before the app can start." }
sec_msg = { desc = "The *secret* message! Auto-generated as 8 hex digits if not set", secret = true, default = "{hex:8}" }

[run.main]
image  = "alpine:latest"
cmd    = ["/bin/sh", "-c", "echo \"The message is $MESSAGE. But the secret message is $SECRET_MESSAGE\"; echo; env; echo"]
restart = "no"
env = { MESSAGE = "${message}", SECRET_MESSAGE = "${sec_msg}" }
```
