## Case Study - Unifi Network Application

The linuxserver.io team provides this helpful Unifi Network Application as a docker image and instructions alongside a sample docker compose file and a script to run in the container on startup: [https://github.com/linuxserver/docker-unifi-network-application](https://github.com/linuxserver/docker-unifi-network-application). This is great, but requries every user to build the "stack" themselves and really understand how it's put together at a deeper level as well as manage application state.

With harbor, this stack can be distributed as an app like so:

```
- io.p2net.unifi-network-application.happ/
  - manifest.toml
  - bin/
    - init_mongo.sh
```
Within it that folder structure, `bin/init_mongo.sh` is the db initialization script the github page recommends you use to run first time setup. The entire Harbor App consists of just that and the manifest file below, which describes how the containers are wired.

**manifest.toml**
```toml
[app]
version = "1.0.0"
author = ...
description = "Unifi Netowrk Application from linuxserver.io"
subdomain = "unifi"
... Other Metadata ....

# This contains parameters that the user can configure.
# In this case, there's only one parameter - it is a "secret" and is also automatically
# generated when the user installs the app.
[config]
mongo_pass = { secret = true, default = "auto" }

# Volumes are where persistant application state lives
[volumes]
db_data     = { kind = "data" }
app_config  = { kind = "data" }
init_script = { kind = "app", src = "bin/init-mongo.sh"}

# This is a "Run Unit", which mapps to a running container. In this case, it's mongodb.
[run.unifi-db]
image = "docker.io/mongo:8.0.11"
volumes = { db_data = "/data/db", init_script = "/docker-entrypoint-initdb.d/init-mongo.sh" }
env = { MONGO_USER = "unifi", MONGO_PASS = "${mongo_pass}", ... }

[run.main]
image = "lscr.io/linuxserver/unifi-network-application:latest"
volumes = { app_config = "/config" }
env = { MONGO_USER = "unifi", MONGO_PASS = "${mongo_pass}", MONGO_HOST = "unifi-db", ... etc }

# Routes are named points of ingress to this stack
# In this case, our stack is requesting that the "main" route be published to the web,
# and we connect to the container over https.
[run.main.routes]
main         = { port = "8443", publish = "web", scheme = "https" }
ap_discovery = { port = "10001:10001/udp" }
...


# Define a command. In this case, it resets the admin account's password to "password"
[[command]]
name = "reset_admin_pass"
desc = "Reset the admin password to 'password'"
command = "mongo --port 27117 ace --eval 'db.admin.update( { "name" : "admin" }, { $set : { "x_shadow" : "$6$ybLXKYjTNj9vv$dgGRjoXYFkw33OFZtBsp1flbCpoFQR7ac8O0FrZixHG.sw2AQmA5PuUbQC/e5.Zu.f7pGuF7qBKAfT/JRZFk8/" } } )'"
run_unit = "unifi-db"
```

With this, a user can run: `harbor fetch io.p2net.unifi-network-application && harbor up unifi-network-application` and have the unif network application up and running quickly.
