# Harbor App Case Study - Unifi Network Application

In this case study, we walk through how I built the `unifi-network-application.happ` in about half an hour using Linuxserver.io's documentation and sample docker stack.

By following along, you will see:

- How habor apps enable distribution of stacks of containers
- How to translate docker compose stacks into harbor's world
- How to provision volumes, secrets, and routes

## Goal - Run Unifi's Network Application so you can manage your wifi

The Unifi Network Application is a piece of software that Ubiquity developed to manage their wifi access points. If you fully buy into their hardware stack, this will run on their hardware. However, they also provide it in a format that can be run on your own hardware (like a raspberry pi, for instance).

Linuxserver.io takes this software, packages it, and distributes it in a container that we can run. However, their instructions require a fair amount of setup and knowledge to actually stand up a robust, "home-production-ready" deployment. The harbor ecosystem solves this for us.

Feel free to skip to the completed harbor app at [apps/unifi-network-application.happ](apps/unifi-network-application.happ).


## Step 1. Make the bare harbor app:
Let's start with a barebones app containing what we already know, placed in our `$harbor/apps` folder:
```
# unifi-network-app.happ/manifest.toml
[app]
version = "0.1.0"
author  = "Demo Author"
description = "The unifi network application"
subdomain = "unifi-admin" # I want this on my network as "https://unifi-admin.<harbor-domain>"

[config]
# There will probably be config, not sure what yet

[volumes]
# I imagine we'll have to put the Application's state somewhere.

[run.main]
# There will be SOME container that has to run here.
```

## Step 1. General overview
You can find the instructions here: [https://github.com/linuxserver/docker-unifi-network-application](https://github.com/linuxserver/docker-unifi-network-application). As of this writing (summer 2026), the instructions contain a few key parts:

### **The Image**: `lscr.io/linuxserver/unifi-network-application:latest`
Looks like it works for both amd64 and arm. Great.

### **Mongodb**
The setup instructions mention that this image expects a mongodb instance, properly configured, and of a supported version:
> Starting with version 8.1 of Unifi Network Application, mongodb 3.6 through 7.0 are supported. Starting with version 9.0 of Unifi Network Application, mongodb 8.0 is also supported.

> Make sure you pin your database image version and do not use latest, as mongodb does not support automatic upgrades between major versions.

> MongoDB >4.4 on X86_64 Hardware needs a CPU with AVX support. Some lower end Intel CPU models like Celeron and Pentium (before Tiger-Lake) more Details: Advanced Vector Extensions - Wikipedia don't support AVX, but you can still use MongoDB 4.4.

It looks like we're going to have two "run units" in this harbor app, and probably two separate volumes, one for each. Let's update the compose file. The instructions suggest using the "official mongodb image"

**Thought**: maybe we should pin versions since it seems like there's some limitations on which versions of the app work with which versions of mongodb. Let's use latest for the application, and pick the most recent mongodb 8 release.

```toml
# manifest.toml additions:

[volumes]
# provision two "data"-class volumes, designed to store important app state.
app_config = { kind = "data" }
db_data    = { kind = "data" }

[run.main]
image = "lscr.io/linuxserver/unifi-network-application:latest"
volumes = { app_config = ... } # Not sure were it needs to mount yet.

[run.unifi-db]
image = "docker.io/mongo:8.3.7"
```

## Step 2. Mongodb requirements

### Mongodb init script
The setup instructions say
> If you are using the official mongodb container, you can create your user using an init-mongo.sh file with the following contents (do not modify; copy/paste as is):
```
#!/bin/bash

if which mongosh > /dev/null 2>&1; then
  mongo_init_bin='mongosh'
else
  mongo_init_bin='mongo'
fi
"${mongo_init_bin}" <<EOF
use ${MONGO_AUTHSOURCE}
db.auth("${MONGO_INITDB_ROOT_USERNAME}", "${MONGO_INITDB_ROOT_PASSWORD}")
db.createUser({
  user: "${MONGO_USER}",
  pwd: "${MONGO_PASS}",
  roles: [
    "clusterMonitor",
    { db: "${MONGO_DBNAME}", role: "dbOwner" },
    { db: "${MONGO_DBNAME}_stat", role: "dbOwner" },
    { db: "${MONGO_DBNAME}_audit", role: "dbOwner" },
    { db: "${MONGO_DBNAME}_restore", role: "dbOwner" }
  ]
})
EOF
```

Hm, that seems important to get right. Let's turn that into a file, `init-mongo.sh` as they suggest and distribute it with our happ. We know we're going to have to add that to our mongo-db run unit. Let's make a new file in our happ: `unifi-network-app.happ/init-mongo.sh` and paste those contents in there directly. The instructions say to mount it at "/docker-entrypoint-initdb.d/init-mongo.sh:ro". Great, now we've got all of the mongo volumes. Let's update the manifest again with the new information.

We're provisioning a new type of volume - an `app` volume. `app` volumes come bundled in the .happ folder. They require a `src` field which is a relative path to a file or folder. `app` volumes are readonly.

Here are the updated sections:

```
[volumes]
db_data     = { kind = "data" }
app_config  = { kind = "data" }
init_script = { kind = "app", src = "init-mongo.sh" }

[run.unifi-db]
image = "docker.io/mongo:8.3.7"
volumes = { db_data = "?", init_script = "/docker-entrypoint-initdb.d/init-mongo.sh" }
```

### Database environment
Looking a bit further down the page, we get a sample docker `compose.yml` snippet for mongodb:

```
  unifi-db:
    image: docker.io/mongo:<version tag>
    container_name: unifi-db
    environment:
      - MONGO_INITDB_ROOT_USERNAME=root
      - MONGO_INITDB_ROOT_PASSWORD=
      - MONGO_USER=unifi
      - MONGO_PASS=
      - MONGO_DBNAME=unifi
      - MONGO_AUTHSOURCE=admin
    volumes:
      - /path/to/data:/data/db
      - /path/to/init-mongo.sh:/docker-entrypoint-initdb.d/init-mongo.sh:ro
    restart: unless-stopped
```

Takeaways:
- The database volume should be mounted at /data/db
- Some env needs to be set.

Updates to `manifest.toml`

```
[config]
# Provision a secret which will be our mongodb password. We don't care what the secret actually
# is, so we leave it as "auto". It will be generated on the first run.
mongo_pass = { desc = "MongoDB Password", secret = true, default = "auto" }

[run.unifi-db]
image = "docker.io/mongo:8.3.7"
volumes = { db_data = "/data/db", init_script = "/docker-entrypoint-initdb.d/init-mongo.sh" }

# Add the environment section here
[run.unifi-db.env]
MONGO_USER = "unifi"
MONGO_PASS = "${mongo_pass}" # pass in the secret we configured here
MONGO_DBNAME = "unifi"
MONGO_AUTHSOURCE = "admin"
```

## Step 3. The full stack

Moving towards the end of the instructions, they provide a full-ish docker `compose.yml` file along with a command to run it via `docker`.

```
---
services:
  unifi-network-application:
    image: lscr.io/linuxserver/unifi-network-application:latest
    container_name: unifi-network-application
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - MONGO_USER=unifi
      - MONGO_PASS=
      - MONGO_HOST=unifi-db
      - MONGO_PORT=27017
      - MONGO_DBNAME=unifi
      - MONGO_AUTHSOURCE=admin
      - MEM_LIMIT=1024 #optional
      - MEM_STARTUP=1024 #optional
      - MONGO_TLS= #optional
    volumes:
      - /path/to/unifi-network-application/data:/config
    ports:
      - 8443:8443
      - 3478:3478/udp
      - 10001:10001/udp
      - 8080:8080
      - 1900:1900/udp #optional
      - 8843:8843 #optional
      - 8880:8880 #optional
      - 6789:6789 #optional
      - 5514:5514/udp #optional
    restart: unless-stopped
```

Lower down, they also provide information on what all of the ports are for. This is helpful for naming our `routes`. We pick the ones we need to port forward and skip ones that aren't relevant to our deployment.

Using that information, we can complete our `manifest`:
```
[app]
version      = "1.0.0"
display_name = "Unifi Network Application"
subdomain    = "unifi"

[config]
mongo_pass = { desc = "MongoDB Password", secret = true, default = "auto" }

[volumes]
db_data     = { kind = "data" }
app_config  = { kind = "data" }
init_script = { kind = "app", src = "init-mongo.sh"}

[run.unifi-db]
image = "docker.io/mongo:8.3.7"
volumes = { db_data = "/data/db", init_script = "/docker-entrypoint-initdb.d/init-mongo.sh" }

[run.unifi-db.env]
MONGO_USER = "unifi"
MONGO_PASS = "${mongo_pass}"
MONGO_DBNAME = "unifi"
MONGO_AUTHSOURCE = "admin"

[run.main]
image = "lscr.io/linuxserver/unifi-network-application:latest"
volumes = { app_config = "/config" }

[run.main.routes]
# This is the only route we need to pubslih
main        = { port = "8443:8443", public = true, scheme = "https" }
stun         = { port = "3478:3478" }
ap_discovery = { port = "10001:10001/udp" }
device_comm  = { port = "8080:8080" }
discover_l2  = { port = "1900:1900/udp" }

[run.main.env]
PUID = "1000"
PGID = "1000"
TZ = "Etc/UTC"
MONGO_USER = "unifi"
MONGO_PASS = "${mongo_pass}"
MONGO_HOST = "unifi-db"
MONGO_PORT = "27017"
MONGO_DBNAME = "unifi"
MONGO_AUTHSOURCE = "admin"
MEM_LIMIT = "1024"
MEM_STARTUP = "1024"
MONGO_TLS = ""
```

## Step 4. Problems with raspberry Pi

When we try to run this on a raspberry pi with `harbor start unifi-network-app`, we notice it doesn't seem to be working. Checking the logs, we find that the database uses extensions that our version of arm on the raspberry pi doesn't support. No problem, we can walk back the version of mongo until we find one that works. Between each test, we call `harbor rm unifi-network-app` to clear out all data and "start fresh".

It turns out that version 8.0.11 is both > 8 and can run on the raspberry pi, so we pin it there. Since we pinned the db, let's pin the unifi-network-application to a version we can confirm works for us as well.

Finally, we end up changing our run units with the pinned versions:
```
[run.unifi-db]
image = "docker.io/mongo:8.0.11"
volumes = { db_data = "/data/db", init_script = "/docker-entrypoint-initdb.d/init-mongo.sh" }

...

[run.main]
image = "lscr.io/linuxserver/unifi-network-application:10.0.162"
volumes = { app_config = "/config" }
```

## Step 5. Publish!
Now that we've got a working happ we can share, we can throw it up on github so other users can `harbor fetch` it!