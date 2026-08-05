# Compose Passthrough Demo

Compose has many options harbor does not model. `[run.<unit>.compose]` copies
whatever you put there verbatim into the service's section of the generated
compose.yml. Keys harbor manages (image, ports, ...) are rejected at parse
time.

This manifest produces a service like:

```yaml
services:
  main:
    image: redis:7-alpine
    # ...
    mem_limit: 256m
    healthcheck:
      test: redis-cli ping || exit 1
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 5s
```

```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Compose Passthrough Demo"
description  = "Uses [run.<unit>.compose] to set compose options harbor doesn't model"

[run.main]
image   = "redis:7-alpine"

[run.main.compose]
mem_limit = "256m"

[run.main.compose.healthcheck]
test         = "redis-cli ping || exit 1"
interval     = "10s"
timeout      = "3s"
retries      = 3 # This remains an integer all the way to compose.yml.
start_period = "5s"
```
