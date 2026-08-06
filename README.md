# harbor ~ container stack management for humans

Harbor is an opinionated runtime and management layer that makes container stacks (ie. docker compose) easy to distribute, inspect, and manage. It was designed for folks who want to spend their time *using* their selfhosted apps instead of *sys-administering* them.

## How does it work? 
Harbor runs and manages "happs", that 1) define a `manifest.toml` which fully describe the container stack and 2) optionally contain any helper scripts or files. A happ is either a `<app_id>.happ` folder or, for small apps, a single `<app_id>.happ.md` markdown file with the same files embedded in code blocks (see [demo-markdown](apps/demo-markdown.happ.md)). Here's a simplified example:

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
main = { port = "8443", publish = "web", scheme = "https" }
```


Use harbor to fetch and start the app:
```
$ harbor fetch github:nepthar/harbor/main/apps/unifi-network-application.happ
$ harbor stage unifi-network-application
$ harbor start unifi-network-application
```

Under the hood, Harbor is using the manifest + your configuration to create a docker compose stack.

**There is no lock-in by design.** If you remove the `harbor` binary from your system, you'll still have an organized, functional folder tree of docker compose stacks that you can directly interact with.

Manifests are small enough to be digested in a few seconds. For a full, functioning example, see my [case study](docs/case_study.md) on the Unifi Network Application where we build the manifest from scratch in a few minutes.

## Why does harbor exist?

- **Distributing self-hosted applications is hard today.**
Each functional docker compose stack is a special snowflake based on the convolution of (your environment) x (the stack requirements), making them nearly impossible to share except in the most trivial cases. The creator can't know what your setup looks like, so there's no universal installable unit. **Harbor makes stack distribution as simple as posting your app on github**

- **Managing container-based stacks is time consuming.**
Once you start using docker compose to run your selfhosted applications, you end up managing each stack individually. It's difficult to version control a "folder of stacks" properly. Each new stack (except for super simple ones) has to be hand-configured and wired in to your system. Oh, and I also HATE `.env` files and docker volumes. **Harbor provides a simple mechanism to store/inspect secrets, application data, and logs. You configure it once, it wires every app automatically**

- **Other solutions exist, but require you to be a sysadmin.**
Sure, you can spin up a kubernetes cluster and go crazy with helm charts, automated CI/CD pipelines, etc, but that takes time and a specialized skillset. **Harbor is designed to be set up and configured in an afternoon and require minimal maintenance**

- **Snapshotting containers SHOULD be trivial but is not.**
Similar to management and deployment, snapshotting your containers requires a manual setup. **In the near future, harbor will support simple snapshotting and restoring for all of your apps.** Since harbor is a homelab-focused tool, we don't require 100% uptime and can snapshot by stopping the stack, making a tarball of all volumes and configuration data, and then starting the service again.

There are GUI options like Portainer and Dockge that help manage containers and stacks, but they basically wrap the problems above in a shiny UI rather than solve them.


## Getting Started:
Preqrequisites: `docker`, `docker compose plugin`, `uv` (and therefore `python`)
1. `$ uv tool install "git+https://github.com/nepthar/harbor"`
2. `$ harbor init`
3. Configure harbor as requested by init (or just leave all defaults)
4. `$ harbor fetch github:nepthar/harbor/main/apps/hello-world.happ.md`
5. `$ harbor start hello-world`
6. `$ harbor logs hello-world`
7. Examine your `apps/` folder to look at how the hello-world example is constructed.

## Creating your own apps
You can create your own harbor app by making (or linking in) a folder in `$harbor/apps/<your_app_id>.happ` and has a `manifest.toml` file. Small apps can instead be a single `$harbor/apps/<your_app_id>.happ.md` markdown file, which keeps the whole app auditable at a glance. Harbor will then recognize it under `<your_app_id>`. To develop against a directory of happs outside `apps/`, add it to your `config.toml` as an extra app source:

```toml
[[app_source]]
name     = "dev"
location = "~/code/happs"
```

The best way to learn is by example and by reading the source (at this stage). Check out [manifest.py](harbor/lib/manifest.py) for the most up to information on what to put in a manifest.


## Audience
At the moment, harbor is a cli tool aimed at making the lives of folks who like to self host a bit easier. You should be comfortable with docker and linux and be OK with alpha-quality software and interested in giving feedback if you notice things break.

In the future, Harbor will target more non technical users by building out a web UI and a few more convenience features. One of the goals is to make a seamless ecosystem of installable, self-hostable apps.

At the moment, I would love feedback on your experience using harbor or creating happs.


## Development status and Roadmap

Harbor is currently in a **pre-beta** stage. I do not yet consider it feature complete for a v1.0 release. I am regularly making structural changes without a forward migration path. You are welcome to join development if you like!

(in rough order of priority)

### Harbor CLI:
* **[in progress] Commands**: `harbor cmd <app> <cmd_name> [... args]` which will run the command <cmd_name> as defined in the manifest. This is nearly complete.
* **FQDN fetch & publish**: Allow fetching by reverse-fqdn app_id. ie: `harbor fetch com.my-company.my-app` will look for a well known index at `https://my-app.mycompany.com/.well-known/happ_versions.txt`, parse it, and fetch a bundle hosted there, using the domain's SSL cert as verification. Optionally, it will pop open the `manifest.toml` of the app you're considering to install so you can verify that you want to download it.
* **Other Route Providers**: Integrate with things like Pangolin, traefik, etc to "publish" routes to. Also, allow for other route publishing domains. ("lan", "vpn", etc). Right now, we only support Nginx Proxy Manager
* **Cron Jobs**: Add the ability to define and execute commands and cron jobs from within the manifest.toml. Think regular admin tasks, database cleanup, password reset, etc.
* **Services & Service Catalog**: Allow happ developers to better focus on their own app by saying "Just give me a postgres instance + login for my app" rather than adding postgres to their stack manually. This `services` system would enable an happ to list the services it "provides" and have other services "require" them. This feature will require a lot of thought.

### Harbor WebUI:
Harbor's goal is to be easy for most technically-minded folks to set up in an afternoon. As such, it will at some point have a web UI, which will allow you to fetch happs, manage them, set configuration parameters and secrets, and manage volumes and snapshots.

### Harbor Runtime & OS
I also wish to make Harbor a simple platform and runtime for writing self-hosted apps. As part of this, I want to make it attractive for folks who want to vibe code "safely". 
- **Supported Happy Path Stacks** - Ruby on Rails, fastAPI, etc. Get up and running with those in seconds.
- **Little Snitch Mode** - A network mode that will limit outgoing connections to those that the harbor user has explicitly allowed. They'll have to manually approve domains via the web UI. This would provide a high level of trust.
- **Operating System**: Over time, I imagine harbor becoming operating-system like, with things akin to syscalls going over a dedicated and secured socket, where apps can request various things from the host system - access to data, additional storage space, users contact information, etc.


## AI Usage Policy
I have nothing against using LLMs as a tool for software developemnt[1], in fact, I've found that LLMs are adept at writing harbor apps. As such, some of the code here (unit tests especially) has been written by LLMs via Cursor & Claude. Most of the codebase, however, was hand written by myself. I found that LLMs made bad architectural decisions and I actually saved time by writing it myself.

As the project progresses, more code (especially the Web UI) will likely get heavy contributions by LLMs. I'm considering committing all AI-generated code under a different github author so you can `git-blame` and have a better view.

At the moment, all of the documentation is written directly by myself, although I feed it through Claude to catch any functional errors or typos. 

[1] - I do, however, *hate* the flood of shitty vibe coded apps by people who have no idea what they're doing, soaring high on inflated egos with models telling them how unique and brilliant they are. LLMs are tools, not "get out of thinking free" cards.

## Philosophy & Bigger Picture
I want to enable more people to **run software like it's 1997**. Back in 1997, you bought a copy of Microsoft Word and MSFT had NO IDEA what crazy manifestos you were writing with it, because it was your copy running on hardware you controlled. You could pull the plug. You didn't lose access to your documents if you stopped paying a subscription. "I'm altering the deal, pray I don't alter it any further" was a fun line from Star Wars, not the *implication* of the latest "update" from Adobe Creative Cloud.

Harbor is part of the P2 project, a batteries-included framework providing an OS-like experience for selfhosted, peer to peer apps
