# harbor ~ container stack management for humans

Harbor is an opinionated runtime and management layer that makes container stacks (ie. docker compose) easy to distribute, inspect, snapshot, and manage. It was designed for folks who want to spend their time *using* their selfhosted apps instead of *sys-administering* them.

## How does it work? 
Harbor runs and manages "happs", that 1) define a `manifest.toml` which fully describes the container stack and 2) optionally contain any helper scripts or files. A happ is either a `<app_id>.happ` folder or, for small apps, a single `<app_id>.happ.md` markdown file with the same files embedded in code blocks (see [demo-markdown](demo-apps/demo-markdown.happ.md)). Here's a simplified example:

unifi-network-application.happ/manifest.toml:
```toml
[app]
description = "Unifi Network Application from linuxserver.io"

[config]
# A secret that the user never has to set, generated on install and stored encrypted.
mongo_pass = { secret = true, default = "auto" }

[volumes]
db_data    = { kind = "data" }
app_config = { kind = "data" }

[run.unifi-db]
image   = "docker.io/mongo:8.0.11"
volumes = { db_data = "/data/db" }
env     = { MONGO_PASS = "${mongo_pass}", ... }

[run.main]
image   = "lscr.io/linuxserver/unifi-network-application:10.4.57"
volumes = { app_config = "/config" }
env     = { MONGO_HOST = "unifi-db", MONGO_PASS = "${mongo_pass}", ... }

[run.main.routes]
main = { port = "8443", scheme = "https" }
```


Install it from the catalog, then start it:
```
$ harbor install unifi-network-application
$ harbor start unifi-network-application
```

Under the hood, Harbor is using the manifest + your configuration to create a docker compose stack.

**There is no lock-in by design.** If you remove the `harbor` binary from your system, you'll still have an organized, functional folder tree of docker compose stacks that you can directly interact with.

Manifests are small enough to be digested in a few seconds. For a full, functioning example, see my [case study](docs/case_study.md) on the Unifi Network Application where we build the manifest from scratch in a few minutes.

## Getting Started:
Prerequisites: `docker`, `docker compose plugin`, `uv` (and therefore `python`)
1. `$ uv tool install "git+https://github.com/nepthar/harbor"`
2. `$ harbor init`
3. Configure harbor as requested by init (or just leave all defaults)
4. `$ harbor start hello-world`
5. `$ harbor logs hello-world`
6. Examine `repos/demos/hello-world.happ.md` to see how the example is constructed.

`harbor init` sets up two repos for you: `staples`, the apps harbor maintains,
and `demos`, small happs that each demonstrate one feature. Remove the second
once you are done exploring: `harbor repo remove demos`.

## Why harbor?

- **Configure your system layout once, install any app**
Harbor builds docker stacks, placing data where you tell it. Apps then can describe "what" they need instead of "how" it's wired up.

- **Distributing self-hosted applications is hard today.**
Harbor makes it trivial to create and use app repositories. It's just a folder pushed to github, and they run on any properly configured install of harbor.

- **Managing container-based stacks is time consuming.**
Once you start using docker compose to run your selfhosted applications, you end up managing each stack individually. It's difficult to version control a "folder of stacks" properly. Each new stack (except for super simple ones) has to be hand-configured and wired in to your system. Oh, and I also HATE `.env` files and docker volumes. **Harbor provides a simple mechanism to store/inspect secrets, application data, and logs. You configure it once, it wires every app automatically**

- **Other solutions exist, but require you to be a sysadmin.**
Harbor is simple to reason about. It is mostly just a bunch of folders and text files.

- **Snapshotting containers SHOULD be trivial in 2026, but is not.**
Since harbor is designed for a homelab setting, it assumes that a few seconds of downtime is an acceptable price for a snapshot you can actually trust. `harbor snapshot <app>` stops the app, archives its volumes and run state together, and starts it again if it was running — so what you get back is a coherent point in time rather than a copy of files that were being written to. Restoring is the same trade in reverse.

There are GUI options like Portainer and Dockge that help manage containers and stacks, but they basically wrap the problems above in a shiny UI rather than solve them.

## Why NOT harbor?
You may not want to use harbor if:

- You regularly run complicated, redundant docker stacks that failover, have more than ~25 concurrent users, or have serious compute requirements.

- You are self-hosting to learn how to use specific technologies like Kubernetes

## See Also


- The anatomy of a harbor app [manifest](docs/manifest.md)
- A [case study](docs/case_study.md): building one from scratch
- Our current [roadmap](docs/roadmap.md)
- How the [test suite](docs/testing.md) is put together



## Philosophy & Bigger Picture
I want to enable more people to **run software like it's 1997**. Back in 1997, you bought a copy of Microsoft Word and MSFT had NO IDEA what crazy manifestos you were writing with it, because it was your copy running on hardware you controlled. You could pull the plug. You didn't lose access to your documents if you stopped paying a subscription. "I'm altering the deal, pray I don't alter it any further" was a fun line from Star Wars, not the *implication* of the latest "update" from Adobe Creative Cloud.

Harbor is part of the P2 project, a batteries-included framework providing an OS-like experience for selfhosted, peer to peer apps
