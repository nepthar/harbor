# Immich - Photo and Video Storage, Sharing, and Backup

> Immich is a self-hosted photo and video management solution. Easily back up,
> organize, and share your photos. ~ [https://immich.app](https://immich.app)

No hardware acceleration yet.

## Installing

The photo library has to be a directory on the host first — harbor binds a path, it does not speak NFS. Mount the share through fstab, autofs, or a systemd `.mount` unit, declare it in config.toml, then bind:

```toml
[host_volume.photos]
path = "/mnt/immich"
```

```
harbor start immich --bind photos=photos
```

## manifest.toml

```toml happ_path="manifest.toml"
[app]
version      = "3.1.0"
display_name = "Immich"
description  = "Self-hosted photo and video backup"
subdomain    = "immich"

[config]
db_password = { desc = "Postgres password", secret = true, default = "{alnum:16}" }

[volumes]
photos = { kind = "host",  desc = "Photo/video library location. bind to a media share" }
db     = { kind = "data", desc = "Postgres data. Note: this must be on a local disk" }
models = { kind = "temp", desc = "ML model cache" }

[run.main]
image   = "ghcr.io/immich-app/immich-server:v3.1.0"
volumes = { photos = "/data" }

[run.main.routes]
main = { port = "2283" }

[run.main.compose]
depends_on = ["redis", "database"]

[run.main.env]
DB_PASSWORD      = "${db_password}"
DB_USERNAME      = "postgres"
DB_DATABASE_NAME = "immich"
# DB_HOSTNAME / REDIS_HOSTNAME default to the sibling unit names below.
# IMMICH_MACHINE_LEARNING_URL defaults to http://immich-machine-learning:3003.

[run.immich-machine-learning]
image   = "ghcr.io/immich-app/immich-machine-learning:v3.1.0"
volumes = { models = "/cache" }

[run.redis]
image = "docker.io/valkey/valkey:9@sha256:8e8d64b405ce18f41b8e5ee20aa4687a8ed0022d1298f2ce31cdcf3a76e09411"

[run.redis.compose.healthcheck]
test = "redis-cli ping || exit 1"

[run.database]
image   = "ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23"
volumes = { db = "/var/lib/postgresql/data" }

[run.database.compose]
shm_size = "128mb"

[run.database.env]
POSTGRES_PASSWORD    = "${db_password}"
POSTGRES_USER        = "postgres"
POSTGRES_DB          = "immich"
POSTGRES_INITDB_ARGS = "--data-checksums"
```

